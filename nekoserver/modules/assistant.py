"""The optional AI assistant (ASSISTANT.md): Ollama runs a language model on this PC and
Open WebUI is its chat page, at home and on the phone through Tailscale.

Setup asks once whether it is wanted. No other step needs it, and nothing that goes wrong
here fails setup: a problem is said ("the AI assistant needs a look", with the reason)
and every other step runs as usual. It runs as a Compose project of its own, on its own
network, with a processor share and a memory limit of its own, at the lowest priority,
and Linux stops it first when memory runs out, so it never slows the other apps down.
"""

import json
import math
import os
import re
import secrets
import shutil
import time
import urllib.request
from pathlib import Path

import account_setup
import account_store
import guided_setup
from neko_common import (
    APPS,
    ASSISTANT,
    atomic_json,
    atomic_write,
    checked_run,
    compose_up,
    ensure_directory,
    host_timezone,
    safe_path,
    verify_storage,
)

DEPENDENCIES = ['docker']
DESCRIPTION = 'Optional AI assistant (Ollama and Open WebUI) in its own project, within set limits'

TITLE = 'AI assistant'
PORT = APPS[ASSISTANT][2]
PROJECT = 'neko-' + ASSISTANT
# The model setup picks by this PC's memory: (smallest memory in GiB, model, its
# download in GB). The biggest that leaves room for every other app; below the last
# row, there is no room for one that is worth having.
MODELS = ((24, 'qwen3.5:9b', 6.6), (12, 'qwen3.5:4b', 3.3))
# A model named by hand under "model" (as Ollama names it: "qwen3.5:4b", "gemma4:12b").
MODEL_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}(?::[a-z0-9][a-z0-9._-]{0,63})?', re.ASCII)
# A model named by hand has no known size: the download and memory are taken as this.
UNKNOWN_MODEL_GB = 7
# Room the SSD must keep: the two images (about 6 GB), plus 10 GB for everything else.
IMAGES_GB = 6
SPARE_GB = 10
# How long the chat page may take to answer after a start (its first start sets up its
# database, which takes a minute or two on a small PC).
START_WAIT = 300
EXPLAIN = (
    'A private AI assistant you can ask questions in your browser and on your phone, like ChatGPT.',
    'It runs on this server only: nothing you ask leaves your home.',
    'It downloads about 10 GB (the programs and the model) and uses part of the memory while it answers.',
    'It never slows down or stops your other apps: they come first.',
    'You can skip this; everything else works without it.',
)


class NotHere(Exception):
    """This PC cannot have the assistant (too little memory or space): not a problem to fix."""


def run(ctx):
    """The setup step. It never raises: whatever stops the assistant is said and left for
    later, so the rest of setup is never held up by it (Ctrl+C still stops setup)."""
    try:
        return _run(ctx)
    except NotHere as exc:
        ctx.log(f'{TITLE}: not set up: {exc}')
        return {'status': 'unavailable', 'reason': str(exc)}
    except Exception as exc:  # noqa: BLE001 - nothing here may fail setup
        reason = f'{TITLE} needs a look: {exc}. Everything else works without it; run setup again once that is fixed'
        ctx.log(reason)
        return {'status': 'needs_setup', 'reason': reason}


def _run(ctx):
    options(ctx.config)
    if not ctx.apply:
        ctx.log(f'{TITLE}: optional; asked during apply')
        return None
    answer = guided_setup.wanted(ctx, ASSISTANT, TITLE, EXPLAIN)
    if answer is None:
        ctx.log(f'{TITLE}: not asked (nobody at the terminal); run  sudo bash install.sh  to choose')
        return None
    if answer is False:
        stop(ctx.config, ctx.log)
        return None
    if os.geteuid() != 0:
        raise RuntimeError('setup must run as root (sudo)')
    verify_storage(ctx.config)
    model, size = chosen_model(ctx.config, memory_gib())
    check_space(ctx.config, size)
    install(ctx, model, size)
    return None


def options(config):
    """The assistant's settings ("assistant" in my-config.json), checked."""
    opts = config.get(ASSISTANT, {})
    if not isinstance(opts, dict):
        raise RuntimeError('"assistant" in the settings must be an object')
    if 'wanted' in opts and type(opts['wanted']) is not bool:
        raise RuntimeError('"wanted" under "assistant" must be true or false')
    model = opts.get('model', 'auto')
    if not isinstance(model, str) or (model != 'auto' and not MODEL_NAME.fullmatch(model)):
        raise RuntimeError('"model" under "assistant" must be "auto" or a model name such as qwen3.5:4b')
    return opts


def memory_gib(meminfo='/proc/meminfo'):
    """This PC's memory in GiB (0 when it cannot be read)."""
    try:
        for line in Path(meminfo).read_text().splitlines():
            if line.startswith('MemTotal:'):
                return int(line.split()[1]) / 1024**2
    except (OSError, ValueError, IndexError):
        pass
    return 0


def chosen_model(config, memory):
    """(model, its size in GB): the one named under "model", else the biggest that fits
    this PC's memory. NotHere when none fits."""
    named = options(config).get('model', 'auto')
    if named != 'auto':
        return named, UNKNOWN_MODEL_GB
    for smallest, model, size in MODELS:
        if memory >= smallest:
            return model, size
    raise NotHere(
        f'it needs at least {MODELS[-1][0]} GB of memory and this PC has {memory:.0f} GB '
        '(to try a smaller model anyway, set "model" under "assistant" in my-config.json, for example qwen3.5:2b)'
    )


def check_space(config, size, usage=shutil.disk_usage):
    """The SSD has room for the images and the model, with room left for everything else."""
    free = usage(config['data_mount']).free / 1000**3
    needed = math.ceil(IMAGES_GB + size + SPARE_GB)
    if free < needed:
        raise NotHere(f'it needs {needed} GB free on the app SSD and there are {free:.0f} GB')


def limits(memory, cores, size):
    """(processor share, memory limit) for the model's container: half the processor
    cores (at least one), and the model's own need or 40 % of the memory, whichever is
    more, never more than 60 %. The chat page has its own small limit in the template."""
    cpus = max(1, (cores or 1) // 2)
    cap = max(math.ceil(size * 1.3 + 1), math.floor(memory * 0.4))
    if memory:
        cap = min(cap, math.floor(memory * 0.6))
    return str(cpus), f'{max(1, cap)}g'


def render(template, values):
    """The Compose file with this server's values in place of ${NEKO_...:-default}."""

    def value(match):
        if match.group(1) not in values:
            raise RuntimeError(f'the assistant template has no value for {match.group(1)}')
        return values[match.group(1)]

    return re.sub(r'\$\{NEKO_([A-Z_]+)(?::-[^}]*)?\}', value, template)


def saved_login(config):
    """The assistant's admin login, from NekoServer's password list (made the first time,
    so `account-setup.py show assistant` shows it like any other app's)."""
    entry = account_store.get(config, ASSISTANT)
    if entry:
        return entry
    username, password = account_store.generate(ASSISTANT)
    store = account_store.load(config)
    store['apps'][ASSISTANT] = {
        'username': username,
        'password': password,
        'state': 'planned',
        'allow_create': True,
        'source': 'generate',
    }
    account_store.save(config, store)
    return store['apps'][ASSISTANT]


def env_file(path, login):
    """The project's private .env: the page's own secret key (kept once made) and the admin
    login, which Open WebUI makes at its first start."""
    key = existing_value(path, 'WEBUI_SECRET_KEY') or secrets.token_urlsafe(32)
    lines = [
        f'WEBUI_SECRET_KEY={key}',
        f'WEBUI_ADMIN_EMAIL={login["username"]}',
        f'WEBUI_ADMIN_PASSWORD={login["password"]}',
    ]
    return '\n'.join(lines) + '\n'


def existing_value(path, key):
    try:
        text = Path(path).read_text()
    except OSError:
        return ''
    found = re.search(rf'^{key}=(.+)$', text, re.MULTILINE)
    return found.group(1).strip() if found else ''


def folders(config):
    """(project folder, models folder, chat folder) on the app SSD."""
    data = Path(config['data_mount'])
    home = data / 'appdata' / ASSISTANT
    return data / 'compose' / ASSISTANT, home / 'models', home / 'chat'


def install(ctx, model, size):
    config = ctx.config
    project, models, chat = folders(config)
    for folder in (project, models, chat):
        safe_path(folder)
        if folder.is_symlink():
            raise RuntimeError(f'refusing a linked folder: {folder}')
        ensure_directory(folder, 0o700)
    login = saved_login(config)
    atomic_write(project / '.env', env_file(project / '.env', login), 0o600)
    cpus, memory = limits(memory_gib(), os.cpu_count(), size)
    template = (Path(__file__).resolve().parents[1] / 'compose' / 'assistant.yaml').read_text(encoding='utf-8')
    values = {
        'DATA': config['data_mount'],
        'CPUS': cpus,
        'MODEL_MEMORY': memory,
        'MODEL': model,
        'TZ': host_timezone(config),
    }
    manifest = project / 'compose.yaml'
    atomic_write(manifest, render(template, values), 0o600)
    base = compose(manifest)
    compose_up(base, TITLE, ctx.log)
    wait_until_answering()
    account_setup.assistant_signs_in(login)
    account_store.update(config, ASSISTANT, state='verified', allow_create=False)
    note = fetch_model(base, model, ctx.log)
    atomic_json(
        Path(config['data_mount']) / 'reports' / 'assistant.json',
        {'format': 'neko-assistant-v1', 'model': model, 'cpus': cpus, 'memory': memory, 'model_ready': note == 'ready'},
    )
    ctx.log(
        f'{TITLE}: running at http://127.0.0.1:{PORT}/ with {model} (at most {cpus} processor cores and {memory} of '
        f'memory, below every other app). Sign in with its login:  {guided_setup.login_command(config, ASSISTANT)}'
    )


def compose(manifest):
    return ['docker', 'compose', '-f', str(manifest), '-p', PROJECT]


def wait_until_answering(opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic, wait=START_WAIT):
    """The chat page answers its health address (its first start takes a minute or two)."""
    deadline = clock() + wait
    while True:
        try:
            with opener(f'http://127.0.0.1:{PORT}/health', timeout=5) as answer:
                if json.loads(answer.read(4000)).get('status') is True:
                    return
        except (OSError, ValueError, AttributeError):
            pass
        if clock() >= deadline:
            raise RuntimeError(f'its chat page did not answer within {wait // 60} minutes')
        sleep(5)


def fetch_model(base, model, log, run=checked_run):
    """'ready' when the model is already there; else its download is started in the
    background ('downloading'): the chat page works meanwhile and offers the model once
    it is in. A download cut off is started again by the next setup run."""
    listed = run([*base, 'exec', '-T', 'ollama', 'ollama', 'list'], 60, f'{TITLE} (ollama list)').stdout
    if any(line.split()[:1] == [model] for line in listed.splitlines()):
        return 'ready'
    run([*base, 'exec', '-d', '-T', 'ollama', 'ollama', 'pull', model], 60, f'{TITLE} (ollama pull)')
    log(f'{TITLE}: downloading the model {model} in the background; it answers once the download is in')
    return 'downloading'


def stop(config, log, run=checked_run):
    """Answered no (or changed to no): the assistant is stopped and its containers removed;
    its chats, model and settings stay on the SSD, so yes later starts it as it was."""
    manifest = folders(config)[0] / 'compose.yaml'
    if not manifest.is_file() or manifest.is_symlink():
        return
    run([*compose(manifest), 'down'], 300, f'{TITLE} (docker compose down)')
    manifest.rename(manifest.with_name('compose.yaml.stopped'))
    log(f'{TITLE}: stopped; its chats and model stay on the SSD (answer yes in setup to start it again)')
