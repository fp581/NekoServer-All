"""The optional AI assistant (ASSISTANT.md): Ollama runs a language model on this PC, and the
dashboard's "Ask Neko" page asks it to explain what the dashboard shows.

Setup asks once, with a choice of the models that fit this PC and what each one costs it;
the answer is "no assistant" unless the person picks one, and a run with nobody at the
terminal never installs it. Before anything is downloaded it checks the processor, the free
memory, the SSD space and the port. No other step needs it, and nothing that goes wrong here
fails setup: a problem is said ("the AI assistant needs a look", with the reason) or the PC
is "not available" for it, and every other step runs as usual. It runs as a Compose project
of its own with a hard memory limit, half the processor cores at most, the lowest priority,
and Linux stops it first when memory runs out, so it never slows the other apps down.

Every check reads kernel files and Python's own library only (/proc/meminfo, /proc/cpuinfo,
platform.machine, a socket, the SSD's free space): no program of a package is run, so no
checking tool can be missing. Docker comes from the docker step, which this one needs.
"""

import json
import math
import os
import platform
import re
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import guided_setup
import importlib.util
from neko_common import (
    ASSISTANT,
    OLLAMA_PORT,
    atomic_json,
    atomic_write,
    checked_run,
    compose_up,
    ensure_directory,
    safe_path,
    verify_storage,
)

DEPENDENCIES = ['docker']
DESCRIPTION = 'Optional AI assistant (Ollama) for the dashboard, in its own project within set limits'

TITLE = 'AI assistant'
# The dashboard PIN (wizard.PIN_FILE): asking Neko is an app action, so it needs one.
PIN_FILE = Path('/etc/nekoserver-control/pin.json')
NO_PIN = (
    'Ask Neko needs a dashboard PIN, and none was chosen; run  sudo bash install.sh  in a terminal '
    'on the server to choose one'
)
# The dashboard collector's settings (dashboard.COLLECTOR_SETTINGS): the dashboard step
# runs first, so the assistant tells it itself that Ask Neko is there.
COLLECTOR_SETTINGS = Path('/etc/nekoserver-dashboard/config.json')
PROJECT = 'neko-' + ASSISTANT
# The models setup offers, smallest first: (model, its download in GB, the memory it needs
# while it answers in GiB, what its answers are like). The memory is the model's own
# memory (the container's anon memory, the weights and the 8192-token context, with the
# model loaded) measured for 0.49.0, 2.2, 3.7 and 6.7 GiB, with 20 % to spare and rounded
# up; it is also the model's hard memory limit, so a model that needs more than that is
# stopped instead of the server running out. The small one is the 4-bit build of the 2b
# (the plain 2b tag is an 8-bit build that needs 2.9 GiB).
MODELS = (
    ('qwen3.5:2b-q4_K_M', 1.9, 3, 'short, simple answers, the quickest, but the weakest'),
    ('qwen3.5:4b', 3.3, 5, 'good everyday answers'),
    ('qwen3.5:9b', 6.6, 9, 'the best answers it can give here, the slowest'),
)
# A model named by hand under "model" (as Ollama names it: "qwen3.5:4b", "gemma4:12b",
# "qwen3.5:2b-q4_K_M": a tag may have capitals, the name has none).
MODEL_NAME = re.compile(r'[a-z0-9][a-z0-9._-]{0,63}(?::[A-Za-z0-9][A-Za-z0-9._-]{0,63})?', re.ASCII)
# A model named by hand has no known size: its download is taken as this for the SSD
# check, and it gets all the memory left for a model, which must be NAMED_MODEL_GIB at least.
UNKNOWN_MODEL_GB = 7
NAMED_MODEL_GIB = 1
# Memory: the assistant gets only what is free with every other app running, less a
# reserve for them to grow into (Immich's face recognition, a video being converted, a
# backup): at least RESERVE_GIB, or RESERVE_SHARE of the memory when that is more. (The
# 0.49.0 trial: limits worked out from the total memory alone added up to more than was
# free, and a 6 GB server stalled under memory pressure while the model loaded.)
RESERVE_GIB = 2
RESERVE_SHARE = 0.2
# How long after the last question the model leaves memory (OLLAMA_KEEP_ALIVE in the template).
IDLE_MINUTES = 5
# Room the SSD must keep: Ollama's image (about 3.4 GB to download, more unpacked), plus
# 10 GB for everything else.
IMAGES_GB = 5
SPARE_GB = 10
# The processors Ollama's image is built for.
MACHINES = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'aarch64', 'arm64': 'aarch64'}
# How long Ollama may take to answer after a start.
START_WAIT = 120
EXPLAIN = (
    'A private assistant inside the dashboard ("Ask Neko"): it explains what the dashboard shows,',
    'reads the logs of a problem with passwords and keys taken out, and can suggest a fix from a short',
    'list that runs only when you press Approve. It runs on this server only: nothing you ask leaves your home.',
    'It never slows down or stops your other apps: they come first, and everything works without it.',
    'The model is downloaded from the Ollama library during setup (so it needs the internet) and kept on the',
    'app SSD (appdata/assistant/models). NekoServer itself and its backups never contain it.',
)


class NotHere(Exception):
    """This PC cannot have the assistant (its processor, too little memory or space, the
    port taken): not a problem to fix, and setup goes on."""


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
    opts = options(ctx.config)
    if not ctx.apply:
        ctx.log(f'{TITLE}: optional; asked during apply')
        return None
    if opts.get('wanted') is False:
        stop(ctx.config, ctx.log)
        return None
    chosen = opts.get('wanted') is True and opts.get('model', 'auto') != 'auto'
    if not chosen and not guided_setup.interactive(ctx):
        ctx.log(f'{TITLE}: not asked (nobody at the terminal); run  sudo bash install.sh  to choose')
        return None
    if os.geteuid() != 0:
        raise RuntimeError('setup must run as root (sudo)')
    verify_storage(ctx.config)
    try:
        processor_ok()
        port_free_for_us(ctx.config)
        total, available = memory_now()
        room = memory_for_model(total, available, own_use_gib(ctx.config))
        if not chosen:
            model = ask_model(ctx, total, room, os.cpu_count())
            if model is None:
                return None
            if model == 'none':
                save_choice(ctx, False, 'auto')
                return None
            save_choice(ctx, True, model)
        model, size, memory = fitting(ctx.config, room)
        check_space(ctx.config, size)
        need_pin(ctx)
    except NotHere:
        # Not here (any more): one that runs is stopped, so it can never squeeze the
        # other apps out of memory or keep a port the person's own program needs.
        stop(ctx.config, ctx.log)
        raise
    install(ctx, model, memory)
    return None


def need_pin(ctx, ask=None):
    """Asking Neko is an app action, like restarting an app, so it needs the dashboard
    PIN on every device, the server too. Without one the page could never answer, so no
    model of several GB is downloaded for it: the PIN is asked here (the dashboard step
    just before lets one skip it), or the assistant is left for later. NotHere while
    there is still none."""
    if PIN_FILE.exists():
        return
    dashboard = dashboard_step()
    binary = dashboard.runtime_binary()
    if not binary.exists():
        raise NotHere('Ask Neko lives on the dashboard, which is not set up; set up the dashboard first')
    if not guided_setup.interactive(ctx):
        raise NotHere(NO_PIN)
    print('\nAsk Neko needs a dashboard PIN; choose one now (the same PIN restarts apps from the dashboard).')
    chosen = dashboard.choose_pin(binary, ctx.log, ask) if ask else dashboard.choose_pin(binary, ctx.log)
    if not chosen:
        raise NotHere(NO_PIN)


def dashboard_step():
    """modules/dashboard.py, which asks the PIN and knows where the control program is."""
    spec = importlib.util.spec_from_file_location('nekoserver_dashboard_step', Path(__file__).with_name('dashboard.py'))
    step = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(step)
    return step


def tell_dashboard(settings=COLLECTOR_SETTINGS):
    """The dashboard shows Ask Neko once its collector knows the assistant is wanted (its
    "assistant" setting, which the dashboard step writes from the choice). This step runs
    after the dashboard's, so the choice is passed on here. Without a dashboard, nothing."""
    if not settings.is_file() or settings.is_symlink():
        return
    cfg = json.loads(settings.read_text(encoding='utf-8'))
    if cfg.get(ASSISTANT) != {'wanted': True}:
        cfg[ASSISTANT] = {'wanted': True}
        atomic_json(settings, cfg)


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


# ---- the checks before anything is downloaded ------------------------------------------


def processor_ok(machine=platform.machine, cpuinfo='/proc/cpuinfo'):
    """The processor is one Ollama's image is built for: 64-bit Intel or AMD with SSE4.2
    (every one made since about 2010), or 64-bit ARM."""
    kind = MACHINES.get(machine().lower())
    if kind is None:
        raise NotHere(f'Ollama runs on 64-bit Intel, AMD or ARM processors, and this one is {machine()}')
    if kind == 'x86_64' and 'sse4_2' not in cpu_flags(cpuinfo):
        raise NotHere('this processor lacks SSE4.2, which Ollama needs')
    return kind


def cpu_flags(cpuinfo='/proc/cpuinfo'):
    try:
        for line in Path(cpuinfo).read_text().splitlines():
            if line.startswith('flags'):
                return set(line.partition(':')[2].split())
    except OSError:
        pass
    return set()


def port_free_for_us(config, connect=socket.create_connection):
    """Port 11434 is free, or ours already (setup run again). A program of the person's own
    there (an Ollama they installed themselves) keeps it: nothing is installed beside it."""
    if (folders(config)[0] / 'compose.yaml').is_file():
        return
    try:
        with connect(('127.0.0.1', OLLAMA_PORT), timeout=2):
            pass
    except OSError:
        return
    raise NotHere(
        f'port {OLLAMA_PORT} on this PC is used by another program (an Ollama of your own, perhaps), '
        'so NekoServer installs no assistant beside it'
    )


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
    """The memory the assistant's own container uses now (setup run again): it counts as
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
    """The memory left for a model, in GiB: what is free now (with the assistant's own use
    counted as free) less the reserve the other apps keep."""
    return available + own - max(RESERVE_GIB, total * RESERVE_SHARE)


def check_space(config, size, usage=shutil.disk_usage):
    """The SSD has room for the image and the model, with room left for everything else.
    Setup run again: they are on the SSD already, so only the room for everything else."""
    free = usage(config['data_mount']).free / 1000**3
    there = (folders(config)[0] / 'compose.yaml').is_file()
    needed = SPARE_GB if there else math.ceil(IMAGES_GB + size + SPARE_GB)
    if free < needed:
        raise NotHere(f'it needs {needed} GB free on the app SSD and there are {free:.0f} GB')


# ---- the choice -------------------------------------------------------------------------


def fits(room):
    """The models that fit in `room`, smallest first."""
    return [m for m in MODELS if m[2] <= room]


def choices(total, room, cores):
    """The lines setup shows: what this PC has, one numbered choice per model that fits
    (the biggest marked as recommended) with what it costs this PC, and one line for each
    one that does not. Returns (lines, {answer: model})."""
    usable = fits(room)
    cpus = max(1, (cores or 1) // 2)
    lines = [
        f'This PC has {total:.0f} GB of memory. With your apps running and room kept for them to grow, '
        f'{max(room, 0):.1f} GB are free for the assistant.',
        '  0) No assistant (press Enter)',
    ]
    answers = {'0': 'none', '': 'none'}
    for number, (model, download, need, answers_like) in enumerate(usable, 1):
        mark = ' (recommended)' if number == len(usable) else ''
        lines += [
            f'  {number}) {model}{mark}: {answers_like}.',
            f'     Downloads {download} GB. While it answers it uses about {need} GB of your {total:.0f} GB',
            f'     (its limit), and up to {cpus} of your {cores or 1} processor cores, so a film Jellyfin converts or',
            f'     new photos in Immich go slower meanwhile. {IDLE_MINUTES} minutes after the last question its memory is free again.',
        ]
        answers[str(number)] = model
    for model, _, need, _ in MODELS[len(usable) :]:
        lines.append(
            f'  Not possible here: {model} needs about {need} GB free; this PC has {max(room, 0):.1f} GB free '
            'with your apps running.'
        )
    return lines, answers


def ask_model(ctx, total, room, cores, prompt=None, output=None):
    """The model the person picks, 'none', or None (Ctrl+D: asked again next time).
    Enter is "no assistant", unless the setup window's switch for it was turned on: then
    Enter takes the recommended model. NotHere when no model fits."""
    if not fits(room):
        raise NotHere(not_enough(room, MODELS[0][2]))
    prompt, output = prompt or input, output or print
    output(f'\n=== Optional: {TITLE} ("Ask Neko" in the dashboard) ===')
    for line in EXPLAIN:
        output(line)
    lines, answers = choices(total, room, cores)
    for line in lines:
        output(line)
    switched_on = options(ctx.config).get('wanted') is True
    default = str(len(fits(room))) if switched_on else '0'
    last = len(answers) - 2
    while True:
        try:
            typed = prompt(f'Which one? (0-{last}, Enter for {answers[default] if switched_on else "no assistant"}): ')
        except EOFError:
            output('')
            return None
        typed = typed.strip() or default
        if typed in answers:
            return answers[typed]
        output(f'Type a number from 0 to {last}, or press Enter.')


def save_choice(ctx, wanted, model):
    opts = ctx.config.setdefault(ASSISTANT, {})
    opts['wanted'] = wanted
    opts['model'] = model
    guided_setup.save_settings(ctx)
    if not wanted:
        ctx.log(
            f'{TITLE}: none chosen. To add one later, set "wanted" to true under "assistant" in my-config.json '
            'and run  sudo bash install.sh  again.'
        )


def fitting(config, room):
    """(model, its download in GB, its memory limit like "5g") for the chosen model, when it
    fits in `room` now. NotHere when it does not (the other apps may have grown)."""
    named = options(config).get('model', 'auto')
    known = {m[0]: m for m in MODELS}
    if named in known:
        model, download, need, _ = known[named]
        if need > room:
            raise NotHere(not_enough(room, need, model))
        return model, download, f'{need}g'
    if room < NAMED_MODEL_GIB:
        raise NotHere(not_enough(room, NAMED_MODEL_GIB, named))
    return named, UNKNOWN_MODEL_GB, f'{math.floor(room)}g'


def not_enough(room, need, model='a model'):
    return (
        f'there is not enough free memory: {model} needs about {need} GB free, and this PC has '
        f'{max(room, 0):.1f} GB free with your apps running and room kept for them'
    )


# ---- installing it ----------------------------------------------------------------------


def render(template, values):
    """The Compose file with this server's values in place of ${NEKO_...:-default}."""

    def value(match):
        if match.group(1) not in values:
            raise RuntimeError(f'the assistant template has no value for {match.group(1)}')
        return values[match.group(1)]

    return re.sub(r'\$\{NEKO_([A-Z_]+)(?::-[^}]*)?\}', value, template)


def folders(config):
    """(project folder, models folder) on the app SSD."""
    data = Path(config['data_mount'])
    return data / 'compose' / ASSISTANT, data / 'appdata' / ASSISTANT / 'models'


def cpus(cores):
    """The model's processor share: half the cores, at least one."""
    return str(max(1, (cores or 1) // 2))


def install(ctx, model, memory):
    config = ctx.config
    project, models = folders(config)
    for folder in (project, models):
        safe_path(folder)
        if folder.is_symlink():
            raise RuntimeError(f'refusing a linked folder: {folder}')
        ensure_directory(folder, 0o700)
    share = cpus(os.cpu_count())
    template = (Path(__file__).resolve().parents[1] / 'compose' / 'assistant.yaml').read_text(encoding='utf-8')
    values = {'DATA': config['data_mount'], 'CPUS': share, 'MODEL_MEMORY': memory}
    manifest = project / 'compose.yaml'
    atomic_write(manifest, render(template, values), 0o600)
    base = compose(manifest)
    compose_up(base, TITLE, ctx.log)
    wait_until_answering()
    note, model_id = fetch_model(base, model, ctx.log)
    report(
        config,
        {'model': model, 'model_id': model_id, 'cpus': share, 'memory': memory, 'model_ready': note == 'ready'},
    )
    tell_dashboard()
    ctx.log(
        f'{TITLE}: running with {model} (at most {share} processor cores and {memory} of memory, below every '
        'other app). Ask it on the dashboard\'s Ask Neko page.'
    )


def report(config, values):
    """What the dashboard shows about the assistant (reports/assistant.json on the SSD)."""
    atomic_json(Path(config['data_mount']) / 'reports' / 'assistant.json', {'format': 'neko-assistant-v1', **values})


def compose(manifest):
    return ['docker', 'compose', '-f', str(manifest), '-p', PROJECT]


def wait_until_answering(opener=urllib.request.urlopen, sleep=time.sleep, clock=time.monotonic, wait=START_WAIT):
    """Ollama answers on this PC (127.0.0.1) once it has started."""
    deadline = clock() + wait
    while True:
        try:
            with opener(f'http://127.0.0.1:{OLLAMA_PORT}/api/version', timeout=5) as answer:
                if json.loads(answer.read(4000)).get('version'):
                    return
        except (OSError, ValueError, AttributeError):
            pass
        if clock() >= deadline:
            raise RuntimeError(f'Ollama did not answer within {wait // 60} minutes')
        sleep(5)


def fetch_model(base, model, log, run=checked_run):
    """('ready', its ID) when the model is already there; else its download from the
    Ollama library is started in the background ('downloading', None): Ask Neko says so
    until it is in, and says it is not downloaded when the download was cut off. The next
    setup run starts it again. The model is never part of NekoServer itself."""
    listed = run([*base, 'exec', '-T', 'ollama', 'ollama', 'list'], 60, f'{TITLE} (ollama list)').stdout
    for line in listed.splitlines():
        fields = line.split()
        if fields[:1] == [model]:
            return 'ready', fields[1] if len(fields) > 1 else None
    run([*base, 'exec', '-d', '-T', 'ollama', 'ollama', 'pull', model], 60, f'{TITLE} (ollama pull)')
    log(
        f'{TITLE}: downloading the model {model} from the Ollama library in the background; Ask Neko answers once it is in'
    )
    return 'downloading', None


def stop(config, log, run=checked_run):
    """No assistant (chosen, or not possible here any more): it is stopped and its container
    removed; its model stays on the SSD, so choosing it again starts it as it was."""
    manifest = folders(config)[0] / 'compose.yaml'
    if not manifest.is_file() or manifest.is_symlink():
        return
    run([*compose(manifest), 'down'], 300, f'{TITLE} (docker compose down)')
    manifest.rename(manifest.with_name('compose.yaml.stopped'))
    report(config, {'model': None, 'stopped': True})
    log(f'{TITLE}: stopped; its model stays on the SSD (choose it in setup again to start it)')
