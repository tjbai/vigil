#!/usr/bin/env python3
"""
Vigil - Development Logs Viewer

A real-time log aggregator for concurrent development processes.
Provides a web interface to view logs from multiple processes with
live updates, search, and filtering capabilities.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Set
import subprocess
import signal

try:
    import aiofiles
    import aiohttp
    from aiohttp import web
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("Installing required packages...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "aiohttp", "aiofiles", "jinja2"])
    import aiofiles
    import aiohttp
    from aiohttp import web
    from jinja2 import Environment, FileSystemLoader

# Configuration
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
LOG_DIR = BASE_DIR / "logs"
PORT = 3333
WS_PORT = PORT

# Ensure directories exist
LOG_DIR.mkdir(exist_ok=True)

# Process configurations
PROCESSES = [
    {"name": "next-dev", "command": ["pnpm", "next-dev"], "color": "#3b82f6"},
    {"name": "fastapi-dev", "command": ["pnpm", "fastapi-dev"], "color": "#10b981"},
    {"name": "hocuspocus", "command": ["pnpm", "hocuspocus"], "color": "#f59e0b"},
    {"name": "inngest-worker", "command": ["pnpm", "inngest-worker"], "color": "#8b5cf6"},
    {"name": "inngest-dev", "command": ["pnpm", "inngest-dev"], "color": "#ef4444"},
]

# Global state
websocket_clients: Set[web.WebSocketResponse] = set()
running_processes: Dict[str, asyncio.subprocess.Process] = {}

# Template environment
jinja_env = Environment(loader=FileSystemLoader(TEMPLATES_DIR))


async def broadcast_log(process_name: str, data: str) -> None:
    """Broadcast log data to all connected WebSocket clients."""
    if websocket_clients:
        message = json.dumps({"process": process_name, "data": data})
        # Remove closed connections
        closed_clients = {ws for ws in websocket_clients if ws.closed}
        websocket_clients.difference_update(closed_clients)
        
        # Send to remaining clients
        if websocket_clients:
            await asyncio.gather(
                *[ws.send_str(message) for ws in websocket_clients],
                return_exceptions=True
            )


async def run_process(proc_config: Dict) -> None:
    """Run a single process and stream its output."""
    name = proc_config["name"]
    command = proc_config["command"]
    log_file = LOG_DIR / f"{name}.log"
    
    print(f"🚀 Starting {name}...")
    
    try:
        # Start the process
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=BASE_DIR.parent  # Run from parent directory
        )
        
        running_processes[name] = process
        
        # Open log file for writing
        async with aiofiles.open(log_file, mode='w') as f:
            # Read output line by line
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                    
                line_str = line.decode('utf-8', errors='replace')
                
                # Write to file
                await f.write(line_str)
                await f.flush()
                
                # Broadcast to WebSocket clients
                await broadcast_log(name, line_str)
            
            # Wait for process to complete
            await process.wait()
            exit_msg = f"\nProcess exited with code {process.returncode}\n"
            await f.write(exit_msg)
            await broadcast_log(name, exit_msg)
            
    except Exception as e:
        error_msg = f"Error running {name}: {e}\n"
        print(f"❌ {error_msg}")
        await broadcast_log(name, error_msg)
    finally:
        if name in running_processes:
            del running_processes[name]


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    """Handle WebSocket connections."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    websocket_clients.add(ws)
    print(f"📱 WebSocket client connected (total: {len(websocket_clients)})")
    
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.ERROR:
                print(f'WebSocket error: {ws.exception()}')
                break
    except Exception as e:
        print(f'WebSocket exception: {e}')
    finally:
        websocket_clients.discard(ws)
        print(f"📱 WebSocket client disconnected (total: {len(websocket_clients)})")
    
    return ws


async def index_handler(request: web.Request) -> web.Response:
    """Serve the main HTML interface."""
    template = jinja_env.get_template('index.html')
    html = template.render(processes=PROCESSES, ws_port=WS_PORT)
    return web.Response(text=html, content_type='text/html')


async def static_handler(request: web.Request) -> web.Response:
    """Serve static files."""
    filename = request.match_info['filename']
    file_path = STATIC_DIR / filename
    
    if not file_path.exists() or not file_path.is_file():
        return web.Response(status=404, text='File not found')
    
    # Determine content type
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


async def log_handler(request: web.Request) -> web.Response:
    """Serve log files."""
    log_name = request.match_info['log_name']
    log_path = LOG_DIR / log_name
    
    if log_path.exists():
        async with aiofiles.open(log_path, mode='r') as f:
            content = await f.read()
        return web.Response(text=content, content_type='text/plain')
    else:
        return web.Response(text='', content_type='text/plain')


async def cleanup_processes() -> None:
    """Clean up running processes."""
    print("\n🧹 Cleaning up processes...")
    
    for name, process in running_processes.items():
        if process and process.returncode is None:
            print(f"🛑 Terminating {name}...")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                print(f"⚡ Force killing {name}...")
                process.kill()
                await process.wait()
            except Exception as e:
                print(f"❌ Error stopping {name}: {e}")


def setup_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    """Set up signal handlers for graceful shutdown."""
    def signal_handler():
        print("\n🛑 Received shutdown signal...")
        loop.create_task(cleanup_processes())
        loop.stop()
    
    if sys.platform != 'win32':
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)


async def start_web_server() -> web.AppRunner:
    """Start the web server."""
    app = web.Application()
    
    # Routes
    app.router.add_get('/', index_handler)
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/static/{filename}', static_handler)
    app.router.add_get('/logs/{log_name}', log_handler)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', PORT)
    await site.start()
    
    print(f"\n🎯 Vigil running at http://localhost:{PORT}")
    print("📋 Available processes:")
    for proc in PROCESSES:
        print(f"   • {proc['name']}")
    print(f"\n⌨️  Keyboard shortcuts:")
    print(f"   • 1-9: Switch tabs")
    print(f"   • Cmd/Ctrl+K: Clear current log")
    print(f"   • Cmd/Ctrl+F: Focus search")
    print(f"   • Cmd/Ctrl+T: Toggle timestamps\n")
    
    return runner


async def main() -> None:
    """Main entry point."""
    loop = asyncio.get_event_loop()
    setup_signal_handlers(loop)
    
    # Start web server
    runner = await start_web_server()
    
    try:
        # Start all processes
        tasks = [run_process(proc) for proc in PROCESSES]
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        pass
    finally:
        await cleanup_processes()
        await runner.cleanup()
        print("👋 Vigil shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
        sys.exit(0)