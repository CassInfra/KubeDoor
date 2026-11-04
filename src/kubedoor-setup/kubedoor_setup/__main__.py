"""Run the first-install HTTP server without importing the master application."""
import os

from aiohttp import web

from .api import create_app
from .bootstrap_credentials import ensure_setup_key
from .kubernetes import Kubernetes
from .repository import Repository
from .runner import InstallRunner


async def application():
    k8s = Kubernetes(os.environ.get('SETUP_NAMESPACE', 'kubedoor'))
    await k8s.open()
    repo = Repository(k8s)
    try:
        key = await ensure_setup_key(repo)
        app = create_app(repo, key=key, runner_factory=InstallRunner)
    except BaseException:
        await k8s.close()
        raise

    async def close_kubernetes(app):
        await k8s.close()

    app.on_cleanup.append(close_kubernetes)
    return app


if __name__ == '__main__':
    web.run_app(application(), host='0.0.0.0', port=8080, access_log=None, print=None)
