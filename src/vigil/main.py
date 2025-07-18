import re
import sys
import json
import asyncio
import signal
import logging
import argparse
from pathlib import Path
from typing import Dict, Set

import aiofiles
import aiohttp
from aiohttp import web
from jinja2 import Environment, FileSystemLoader

class VigilServer:
    def __init__(self, config_file, max_lines, port):
        self.config_file = config_file
        self.max_lines = max_lines
        self.port = port
        self.base_dir = Path(__file__).parent.parent.parent
        self.templates_dir = self.base_dir / 'templates'
        self.static_dir = self.base_dir / 'static'
        self.log_dir, self.processes = load_config(config_file)

        self.processes.insert(0, {
            'name': 'vigil',
            'logFile': 'vigil.log'
        })

        self.log_dir.mkdir(exist_ok=True, parents=True)

        logging.basicConfig(
            level=logging.INFO,
            format='%(message)s',
            handlers=[logging.FileHandler(self.log_dir / 'vigil.log', mode='w')]
        )
        self.logger = logging.getLogger('vigil')

        self.logger.info(f'Log directory: {self.log_dir.absolute()}')
        self.logger.info(f'Monitoring {len(self.processes)} processes (max {max_lines} lines each):')
        for proc in self.processes:
            log_path = self.log_dir / proc['logFile']
            exists = '✅' if log_path.exists() else '❌'
            self.logger.info(f'\t{exists} {proc['name']} -> {log_path}')

        self.websocket_clients: Set[web.WebSocketResponse] = set()
        self.running_processes: Dict[str, asyncio.subprocess.Process] = {}
        self.file_positions: Dict[str, int] = {}

        self.jinja_env = Environment(loader=FileSystemLoader(self.templates_dir))

        self.ansi_color_map = {
            30: '#000000', 31: '#e74c3c', 32: '#2ecc71', 33: '#f39c12',
            34: '#3498db', 35: '#9b59b6', 36: '#1abc9c', 37: '#ecf0f1',
            90: '#7f8c8d', 91: '#e74c3c', 92: '#2ecc71', 93: '#f39c12',
            94: '#3498db', 95: '#9b59b6', 96: '#1abc9c', 97: '#ffffff'
        }

    def clean_ansi_codes(self, text: str) -> str:
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)

    async def monitor_log_files(self) -> None:
        while True:
            for process in self.processes:
                process_name = process['name']
                log_file = self.log_dir / process['logFile']
                if not log_file.exists(): continue

                try:
                    current_size = log_file.stat().st_size
                    last_position = self.file_positions.get(process_name, 0)

                    if current_size > last_position:
                        async with aiofiles.open(log_file, mode='r') as f:
                            await f.seek(last_position)
                            new_content = await f.read()

                        if new_content:
                            cleaned_content = self.clean_ansi_codes(new_content)
                            self.logger.debug(f'Broadcasting {len(cleaned_content)} chars from {process_name}')
                            await self.broadcast_log(process_name, cleaned_content)

                        self.file_positions[process_name] = current_size

                except (OSError, IOError):
                    continue

            await asyncio.sleep(0.1)

    async def broadcast_log(self, process_name: str, data: str) -> None:
        if self.websocket_clients:
            message = json.dumps({'process': process_name, 'data': data})
            closed_clients = {ws for ws in self.websocket_clients if ws.closed}
            self.websocket_clients.difference_update(closed_clients)
            if self.websocket_clients:
                await asyncio.gather(
                    *[ws.send_str(message) for ws in self.websocket_clients],
                    return_exceptions=True
                )

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        self.websocket_clients.add(ws)
        self.logger.info(f'Client connected (total: {len(self.websocket_clients)})')

        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.ERROR:
                    self.logger.warning(f'WebSocket error: {ws.exception()}')
                    break
        except Exception as e:
            self.logger.error(f'WebSocket exception: {e}')
        finally:
            self.websocket_clients.discard(ws)
            self.logger.info(f'Client client disconnected (total: {len(self.websocket_clients)})')

        return ws

    async def index_handler(self, request: web.Request) -> web.Response:
        template = self.jinja_env.get_template('index.html')
        html = template.render(processes=self.processes, ws_port=self.port, max_lines=self.max_lines)
        return web.Response(text=html, content_type='text/html')

    async def static_handler(self, request: web.Request) -> web.Response:
        filename = request.match_info['filename']
        file_path = self.static_dir / filename

        if not file_path.exists() or not file_path.is_file():
            return web.Response(status=404, text='File not found')

        content_type = 'text/plain'
        if filename.endswith('.css'):
            content_type = 'text/css'
        elif filename.endswith('.js'):
            content_type = 'application/javascript'
        elif filename.endswith('.html'):
            content_type = 'text/html'

        async with aiofiles.open(file_path, mode='r') as f:
            content = await f.read()

        return web.Response(text=content, content_type=content_type)

    async def log_handler(self, request: web.Request) -> web.Response:
        log_name = request.match_info['log_name']

        process_name = log_name.replace('.log', '')
        process_config = next((p for p in self.processes if p['name'] == process_name), None)

        log_path = (
            self.log_dir / process_config['logFile']
            if process_config
            else self.log_dir / log_name
        )

        if log_path.exists():
            async with aiofiles.open(log_path, mode='r') as f:
                content = await f.read()
            return web.Response(text=content, content_type='text/plain')
        else:
            return web.Response(text='', content_type='text/plain')

    async def cleanup_processes(self) -> None:
        self.logger.info('\nCleaning up processes...')
        for name, process in self.running_processes.items():
            if process and process.returncode is None:
                self.logger.info(f'Terminating {name}...')
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.logger.info(f'Force killing {name}...')
                    process.kill()
                    await process.wait()
                except Exception as e:
                    self.logger.info(f'Error stopping {name}: {e}')

    def setup_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        def signal_handler():
            self.logger.info('\nReceived shutdown signal...')
            loop.create_task(self.cleanup_processes())
            loop.stop()

        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)

    async def serve(self) -> web.AppRunner:
        app = web.Application()

        app.router.add_get('/', self.index_handler)
        app.router.add_get('/ws', self.websocket_handler)
        app.router.add_get('/static/{filename}', self.static_handler)
        app.router.add_get('/logs/{log_name}', self.log_handler)

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', self.port)
        await site.start()

        self.logger.info(f'Vigil running at http://localhost:{self.port}')
        self.logger.info('Available processes:')
        for proc in self.processes:
            self.logger.info(f'\t- {proc['name']}')
        self.logger.info(f'\nKeyboard shortcuts:')
        self.logger.info(f'\t- 1-9: Switch tabs')
        self.logger.info(f'\t- Cmd/Ctrl+K: Clear current log')
        self.logger.info(f'\t- Cmd/Ctrl+F: Focus search')

        return runner

    async def run(self) -> None:
        loop = asyncio.get_event_loop()
        self.setup_signal_handlers(loop)
        runner = await self.serve()

        try:
            self.logger.info('Running in viewer-only mode. Monitoring log files...')
            monitor_task = asyncio.create_task(self.monitor_log_files())
            await asyncio.Event().wait()

        except KeyboardInterrupt:
            self.logger.info('\nReceived shutdown signal...')
        finally:
            await self.cleanup_processes()
            await runner.cleanup()
            self.logger.info('Vigil shutdown complete')

def find_config(config_arg=None):
    if config_arg:
        config_path = Path(config_arg)
        if not config_path.exists():
            print(f'Config file not found: {config_path}')
            sys.exit(1)
        return config_path

    if (config := Path.cwd() / 'vigil-config.json').exists():
        return config

    if (parent_config := Path.cwd().parent / 'vigil-config.json').exists():
        return parent_config

    print('No vigil-config.json found in current directory or parent directory')
    print('Please create a vigil-config.json file or specify one with --config')
    sys.exit(1)

def load_config(config_file):
    with open(config_file) as f:
        config = json.load(f)
    log_dir = (config_file.parent / config['logDir']).resolve()
    processes = config['processes']
    return log_dir, processes

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=str)
    parser.add_argument('--max-lines', type=int, default=1000)
    parser.add_argument('--port', type=int, default=3333)
    args = parser.parse_args()

    config_file = find_config(args.config)
    server = VigilServer(config_file, args.max_lines, args.port)

    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print('\nGoodbye!')
        sys.exit(0)

if __name__ == '__main__':
    main()
