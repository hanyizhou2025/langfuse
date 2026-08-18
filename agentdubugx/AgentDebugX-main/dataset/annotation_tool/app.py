"""Dependency-free HTTP application for the annotation workbench."""

from __future__ import annotations

import argparse
from io import BytesIO
from functools import partial
import json
import logging
import mimetypes
import os
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlsplit
from zipfile import ZIP_DEFLATED, ZipFile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .demo import build_demo_dataset
from .repository import AnnotationRepository, SourceSnapshotConflict
from .source import load_clickhouse_cases
from .validation import AnnotationValidationError


LOGGER = logging.getLogger('agentdebug.dataset.annotation_tool')
STATIC_DIR = Path(__file__).resolve().parent / 'static'
Loader = Callable[[], List[Dict[str, Any]]]


class AnnotationApplication:
    """Route requests to a local repository and a refreshable source loader."""

    def __init__(
        self,
        repository: AnnotationRepository,
        loader: Loader,
        *,
        annotator_id: str,
        demo_mode: bool,
        access_token: Optional[str] = None,
    ) -> None:
        self.repository = repository
        self.loader = loader
        self.annotator_id = annotator_id
        self.demo_mode = demo_mode
        self.access_token = access_token
        self._sync_lock = Lock()

    def sync(self) -> Dict[str, Any]:
        if not self._sync_lock.acquire(blocking=False):
            raise RuntimeError('A source sync is already running.')
        try:
            cases = self.loader()
            count = self.repository.replace_cases(cases)
            return {'ok': True, 'case_count': count}
        finally:
            self._sync_lock.release()

    def handle(
        self,
        method: str,
        path: str,
        query: Mapping[str, List[str]],
        body: Optional[Mapping[str, Any]],
    ) -> 'Response':
        if method == 'GET' and path == '/api/health':
            return Response.json(
                {
                    'status': 'ok',
                    'demo_mode': self.demo_mode,
                    'annotator_id': self.annotator_id,
                }
            )
        if method == 'GET' and path == '/api/config':
            return Response.json(
                {
                    'demo_mode': self.demo_mode,
                    'annotator_id': self.annotator_id,
                    'prediction_visible': False,
                    'schema_version': 'failure-attribution-annotation-v2',
                    'label_management': {
                        'revision_detail': True,
                        'jsonl_import': True,
                        'quality_report': True,
                        'adjudication': True,
                        'dataset_freeze': True,
                    },
                }
            )
        if method == 'GET' and path == '/api/cases':
            status_values = query.get('status')
            status = status_values[0] if status_values else None
            return Response.json({'cases': self.repository.list_cases(status)})
        if method == 'POST' and path == '/api/sync':
            return Response.json(self.sync())
        if method == 'GET' and path == '/api/quality':
            return Response.json(self.repository.quality_report())
        if method == 'POST' and path == '/api/import':
            request = dict(body or {})
            annotations = request.get('annotations')
            if not isinstance(annotations, list) or not all(
                isinstance(item, Mapping) for item in annotations
            ):
                return Response.error(
                    400,
                    {'annotations': 'A JSON array of annotations is required.'},
                )
            return Response.json(
                self.repository.import_annotations(
                    annotations,
                    annotator_id=self.annotator_id,
                    dry_run=bool(request.get('dry_run', True)),
                )
            )
        if method == 'POST' and path == '/api/freeze':
            request = dict(body or {})
            dataset_version = str(request.get('dataset_version') or '').strip()
            dry_run = bool(request.get('dry_run', True))
            result = self.repository.freeze_dataset(
                dataset_version=dataset_version,
                annotator_id=self.annotator_id,
                dry_run=dry_run,
                test_ratio=float(request.get('test_ratio', 0.4)),
            )
            if dry_run or not result.get('can_freeze'):
                return Response.json(result)
            archive = BytesIO()
            with ZipFile(archive, 'w', compression=ZIP_DEFLATED) as bundle:
                for name, content in dict(result.get('files') or {}).items():
                    bundle.writestr(name, str(content).encode('utf-8'))
            return Response(
                status=200,
                body=archive.getvalue(),
                content_type='application/zip',
                headers={
                    'Content-Disposition': (
                        'attachment; filename="%s.zip"' % dataset_version
                    )
                },
            )
        if method == 'GET' and path == '/api/export':
            return Response(
                status=200,
                body=self.repository.export_jsonl().encode('utf-8'),
                content_type='application/x-ndjson; charset=utf-8',
                headers={
                    'Content-Disposition': 'attachment; filename="annotations.jsonl"'
                },
            )
        if path.startswith('/api/cases/'):
            suffix = path[len('/api/cases/') :]
            if '/annotations/' in suffix and method == 'GET':
                case_part, revision_part = suffix.rsplit('/annotations/', 1)
                case_id = unquote(case_part.rstrip('/'))
                try:
                    revision = int(revision_part.rstrip('/'))
                except ValueError:
                    return Response.error(
                        400, {'revision': 'Revision must be an integer.'}
                    )
                return Response.json(
                    {
                        'annotation': self.repository.get_annotation(
                            case_id,
                            revision,
                        )
                    }
                )
            if suffix.endswith('/annotations'):
                case_id = unquote(suffix[: -len('/annotations')].rstrip('/'))
                if method == 'POST':
                    request = dict(body or {})
                    annotation = request.get('annotation')
                    if not isinstance(annotation, Mapping):
                        return Response.error(
                            400, {'annotation': 'A JSON annotation object is required.'}
                        )
                    if str(annotation.get('case_id') or '') != case_id:
                        return Response.error(
                            400, {'case_id': 'Route and annotation case IDs differ.'}
                        )
                    saved = self.repository.save_annotation(
                        annotation,
                        annotator_id=self.annotator_id,
                        change_reason=_optional_text(request.get('change_reason')),
                        expected_source_hash=_optional_text(
                            request.get('expected_source_hash')
                        ),
                    )
                    return Response.json({'ok': True, 'annotation': saved}, status=201)
            elif method == 'GET':
                case_id = unquote(suffix.rstrip('/'))
                return Response.json(self.repository.get_case(case_id))
        return Response.error(404, {'path': 'Unknown API endpoint.'})


class Response:
    def __init__(
        self,
        *,
        status: int,
        body: bytes,
        content_type: str,
        headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = dict(headers or {})

    @classmethod
    def json(cls, payload: Any, *, status: int = 200) -> 'Response':
        return cls(
            status=status,
            body=json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'),
            content_type='application/json; charset=utf-8',
        )

    @classmethod
    def error(cls, status: int, field_errors: Mapping[str, str]) -> 'Response':
        return cls.json(
            {'error': 'request_failed', 'field_errors': dict(field_errors)},
            status=status,
        )


class AnnotationRequestHandler(BaseHTTPRequestHandler):
    server_version = 'AgentDebugXAnnotation/1.0'

    def __init__(self, *args: Any, application: AnnotationApplication, **kwargs: Any):
        self.application = application
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        self._dispatch('GET')

    def do_POST(self) -> None:
        self._dispatch('POST')

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._security_headers()
        self.end_headers()

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/api/'):
            if self.application.access_token and self.headers.get(
                'Authorization'
            ) != 'Bearer %s' % self.application.access_token:
                self._write(Response.error(401, {'authorization': 'Invalid token.'}))
                return
            try:
                body = self._read_json() if method == 'POST' else None
                response = self.application.handle(
                    method,
                    parsed.path,
                    parse_qs(parsed.query),
                    body,
                )
            except AnnotationValidationError as error:
                response = Response.error(422, error.field_errors)
            except SourceSnapshotConflict as error:
                response = Response.error(409, {'source_snapshot': str(error)})
            except KeyError:
                response = Response.error(404, {'case_id': 'Unknown Case ID.'})
            except (ValueError, RuntimeError) as error:
                response = Response.error(400, {'request': str(error)})
            except json.JSONDecodeError:
                response = Response.error(400, {'body': 'Request body is not valid JSON.'})
            except Exception:
                LOGGER.exception('Unhandled annotation API error')
                response = Response.error(500, {'server': 'Unexpected server error.'})
            self._write(response)
            return
        self._serve_static(parsed.path)

    def _read_json(self) -> Mapping[str, Any]:
        content_length = int(self.headers.get('Content-Length') or '0')
        if content_length > 2_000_000:
            raise ValueError('Request body exceeds 2 MB.')
        raw = self.rfile.read(content_length)
        if not raw:
            return {}
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, Mapping):
            raise ValueError('Request body must be a JSON object.')
        return value

    def _serve_static(self, path: str) -> None:
        relative = 'index.html' if path in {'', '/'} else unquote(path.lstrip('/'))
        candidate = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in candidate.parents or not candidate.is_file():
            self._write(Response.error(404, {'path': 'Asset not found.'}))
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        self._write(
            Response(
                status=200,
                body=candidate.read_bytes(),
                content_type=content_type,
            )
        )

    def _write(self, response: Response) -> None:
        self.send_response(response.status)
        self.send_header('Content-Type', response.content_type)
        self.send_header('Content-Length', str(len(response.body)))
        for key, value in response.headers.items():
            self.send_header(key, value)
        self._security_headers()
        self.end_headers()
        self.wfile.write(response.body)

    def _security_headers(self) -> None:
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')

    def log_message(self, format_value: str, *args: Any) -> None:
        LOGGER.info('%s - %s', self.address_string(), format_value % args)


def serve(
    application: AnnotationApplication,
    *,
    host: str,
    port: int,
) -> None:
    if host not in {'127.0.0.1', 'localhost', '::1'} and not application.access_token:
        raise ValueError(
            'AGENTDEBUG_ANNOTATION_TOKEN is required when binding beyond loopback.'
        )
    handler = partial(AnnotationRequestHandler, application=application)
    server = ThreadingHTTPServer((host, port), handler)
    LOGGER.info('Annotation tool listening on http://%s:%d', host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def build_application(args: argparse.Namespace) -> AnnotationApplication:
    db_path = Path(args.db).expanduser().resolve()
    if args.demo:
        loader: Loader = build_demo_dataset
    else:
        loader = partial(
            load_clickhouse_cases,
            config_path=Path(args.config).expanduser().resolve() if args.config else None,
            limit=args.limit,
            rule_id=args.rule_id,
            max_observations_per_trace=args.max_observations,
        )
    application = AnnotationApplication(
        AnnotationRepository(db_path),
        loader,
        annotator_id=os.environ.get('AGENTDEBUG_ANNOTATOR_ID', 'local-annotator'),
        demo_mode=bool(args.demo),
        access_token=os.environ.get('AGENTDEBUG_ANNOTATION_TOKEN'),
    )
    application.sync()
    return application


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the portable AgentDebugX File Not Found annotation tool.'
    )
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8790)
    parser.add_argument(
        '--db',
        default=str(Path(__file__).resolve().parents[1] / 'data' / 'annotations.sqlite'),
    )
    parser.add_argument('--config', help='Optional non-secret ClickHouse table mapping JSON')
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--rule-id', default='file-not-found-v1')
    parser.add_argument('--max-observations', type=int, default=10_000)
    parser.add_argument('--demo', action='store_true')
    parser.add_argument('--log-level', default='INFO')
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    application = build_application(args)
    serve(application, host=args.host, port=args.port)
    return 0


def _optional_text(value: Any) -> Optional[str]:
    text = str(value or '').strip()
    return text or None


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
