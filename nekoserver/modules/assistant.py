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
import subprocess
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
# The models setup picks from: (model, its download in GB, the memory it needs to answer
# in GiB). The biggest whose need fits in the memory left for it (memory_for_model).
MODELS = (('qwen3.5:9b', 6.6, 9), ('qwen3.5:4b', 3.3, 5))
# A model named by hand under "model" (as Ollama names it: "qwen3.5:4b", "gemma4:12b").
MODEL_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}(?::[a-z0-9][a-z0-9._-]{0,63})?', re.ASCII)
# A model named by hand has no known size: its download is taken as this for the SSD
# check, and it gets all the memory left for a model, which must be NAMED_MODEL_GIB at least.
UNKNOWN_MODEL_GB = 7
NAMED_MODEL_GIB = 1
# Memory: the assistant gets only what is free with every other app running, less a
# reserve for them to grow into (Immich's face recognition, a video being converted, a
# backup): at least RESERVE_GIB, or RESERVE_SHARE of the memory when that is more. The
# chat page gets CHAT_GIB of it, the model the rest. (The 0.49.0 trial: limits worked
# out from the total memory alone added up to more than was free, and a 6 GB server
# stalled under memory pressure while the model loaded, every app with it.)
RESERVE_GIB = 2
RESERVE_SHARE = 0.2
CHAT_GIB = 1.5
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
    try:
        room = memory_for_model(*memory_now(), own_use_gib(ctx.config))
        model, size, memory = chosen_model(ctx.config, room)
        check_space(ctx.config, size)
    except NotHere:
        # Not enough room (any more): one that runs is stopped, so it can never squeeze
        # the other apps out of memory.
        stop(ctx.config, ctx.log)
        raise
    install(ctx, model, memory)
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


def memory_now(meminfo='/proc/meminfo'):
    """(total, available) memory in GiB, as Linux counts it (0, 0 when it cannot be read)."""
    found = {}
    try:
        for line in Path(meminfo).read_text().splitlines():
            key, _, rest = line.partition(':')
            if key in ('MemTotal', 'MemAvailable'):
                found[key] = int(rest.split()[0]) / 1024**2
    except (OSError, ValueError, IndexError):
        return 0, 0
    return found.get('MemTotal', 0), found.get('MemAvailable', 0)


def own_use_gib(config, run=checked_run):
    """The memory the assistant's own containers use now (setup run again): it counts as
    free for it, or each run would leave it less. 0 when it is not running."""
    manifest = folders(config)[0] / 'compose.yaml'
    if not manifest.is_file():
        return 0
    try:
        ids = run([*compose(manifest), 'ps', '-q'], 60, f'{TITLE} (docker compose ps)').stdout.split()
        if not ids:
            return 0
        shown = run(['docker', 'stats', '--no-stream', '--format', '{{.MemUsage}}', *ids], 60, TITLE).stdout
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return 0
    return sum(size_gib(line.split('/')[0]) for line in shown.splitlines())


def size_gib(text):
    """Docker's "879.6MiB" or "1.2GiB" in GiB (0 for anything else)."""
    found = re.fullmatch(r'\s*([\d.]+)\s*([KMG])i?B\s*', text)
    if not found:
        return 0
    return float(found.group(1)) * {'K': 1024**-2, 'M': 1024**-1, 'G': 1}[found.group(2)]


def memory_for_model(total, available, own=0):
    """The memory left for the model, in GiB: what is free now (with the assistant's own
    use counted as free) less the reserve the other apps keep and the chat page's share."""
    reserve = max(RESERVE_GIB, total * RESERVE_SHARE)
    return available + own - reserve - CHAT_GIB


def chosen_model(config, room):
    """(model, its download in GB, its memory limit like "5g"): the one named under "model",
    else the biggest whose need fits in `room`. NotHere when there is no room for one."""
    named = options(config).get('model', 'auto')
    if named != 'auto':
        if room < NAMED_MODEL_GIB:
            raise NotHere(not_enough(room, NAMED_MODEL_GIB))
        return named, UNKNOWN_MODEL_GB, f'{math.floor(room)}g'
    for model, size, need in MODELS:
        if room >= need:
            return model, size, f'{math.floor(room)}g'
    raise NotHere(
        not_enough(room, MODELS[-1][2])
        + ' (a smaller model can be named: "model" under "assistant" in my-config.json, for example qwen3.5:2b)'
    )


def not_enough(room, need):
    return (
        f'there is not enough free memory: the model needs {need} GB, and {max(room, 0):.1f} GB are left '
        'once the other apps have theirs, with room to spare for them'
    )


def check_space(config, size, usage=shutil.disk_usage):
    """The SSD has room for the images and the model, with room left for everything else.
    Setup run again: they are on the SSD already, so only the room for everything else."""
    free = usage(config['data_mount']).free / 1000**3
    there = (folders(config)[0] / 'compose.yaml').is_file()
    needed = SPARE_GB if there else math.ceil(IMAGES_GB + size + SPARE_GB)
    if free < needed:
        raise NotHere(f'it needs {needed} GB free on the app SSD and there are {free:.0f} GB')


def cpus(cores):
    """The model's processor share: half the cores, at least one."""
    return str(max(1, (cores or 1) // 2))


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


def install(ctx, model, memory):
    config = ctx.config
    project, models, chat = folders(config)
    for folder in (project, models, chat):
        safe_path(folder)
        if folder.is_symlink():
            raise RuntimeError(f'refusing a linked folder: {folder}')
        ensure_directory(folder, 0o700)
    login = saved_login(config)
    atomic_write(project / '.env', env_file(project / '.env', login), 0o600)
    share = cpus(os.cpu_count())
    template = (Path(__file__).resolve().parents[1] / 'compose' / 'assistant.yaml').read_text(encoding='utf-8')
    values = {
        'DATA': config['data_mount'],
        'CPUS': share,
        'MODEL_MEMORY': memory,
        'CHAT_MEMORY': f'{int(CHAT_GIB * 1024)}m',
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
        {
            'format': 'neko-assistant-v1',
            'model': model,
            'cpus': share,
            'memory': memory,
            'model_ready': note == 'ready',
        },
    )
    ctx.log(
        f'{TITLE}: running at http://127.0.0.1:{PORT}/ with {model} (at most {share} processor cores and {memory} of '
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
