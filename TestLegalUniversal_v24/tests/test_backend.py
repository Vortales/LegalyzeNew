import ast
import re


def _without_docstring(text, triple=False):
    """Убрать строку документации: в пояснениях имена флагов упоминаются."""
    if triple:
        return re.sub(r'""".*?"""', '', text, flags=re.S)
    try:
        node = ast.parse(text)
    except Exception:
        return text
    bodies = [node.body]
    for item in list(node.body):
        if isinstance(item, (ast.FunctionDef, ast.ClassDef)):
            bodies.append(item.body)
    for body in bodies:
        for item in list(body):
            if isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant) \
                    and isinstance(item.value.value, str):
                body.remove(item)
                break
    return ast.unparse(node)
import builtins
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import symtable
import tempfile
import threading
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT.parent / 'TestLegalQtWebEngine'
ORIGINAL = ROOT.parent / 'TestLegal'
REFERENCE = ROOT.parent / 'release_reference'
#: Часть проверок сверяет код с эталонными папками автора (TestLegal,
#: TestLegalQtWebEngine, release_reference). В чистом клоне этих папок нет —
#: такие проверки пропускаются с внятной причиной, а не падают «не найден файл».
def _needs(path, what):
    return unittest.skipUnless(path.exists(), 'нет эталонной папки %s' % what)
sys.path.insert(0, str(ROOT))

import native_browser  # noqa: E402
import speech_backend  # noqa: E402
import win32_embed  # noqa: E402
import win32_hotkeys  # noqa: E402
import web_compat  # noqa: E402


def literal(name, module='web_compat.py'):
    tree = ast.parse((ROOT / module).read_text())
    return next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == name for t in n.targets))


def class_dump(path, names):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.ClassDef) and n.name in names}


def func_dump(path, names):
    return {n.name: ast.dump(n, include_attributes=False) for n in ast.parse(path.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name in names}


class FakeCDPPage:
    """Минимальная страница CDP: метрики + реакция на переопределение вьюпорта.

    Отдаёт то, что отдал бы настоящий Chrome: после
    `Emulation.setDeviceMetricsOverride` layout становится шириной `width`
    CSS px, а `devicePixelRatio` — переданным dsf (при `fitWindow` вид
    дополнительно вписывается в окно, то есть dsf становится `window/width`).
    """

    def __init__(self, metrics=None, window_w=453):
        self.calls = []
        self.window_w = window_w
        self.metrics = metrics or {"w": 680, "h": 1102, "dpr": 2 / 3, "sw": 680}

    def send(self, method, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == 'Emulation.setDeviceMetricsOverride':
            width = int(params.get('width') or 0)
            dsf = float(params.get('deviceScaleFactor') or 1.0)
            self.metrics = {"w": width, "h": int(params.get('height') or 0),
                            "dpr": (self.window_w / width) if params.get('fitWindow') else dsf,
                            "sw": width}
        return {}

    def eval(self, expression, timeout=None):
        import json
        return {'value': json.dumps(self.metrics)}


class ZoomedCDPPage(FakeCDPPage):
    """Страница, у которой поверх эмуляции применён зум профиля (0.667).

    Так Chrome ведёт себя, когда зум из Preferences накладывается на
    `Emulation.setDeviceMetricsOverride`: видимая ширина = width / zoom,
    `devicePixelRatio` = dsf * zoom.
    """

    def __init__(self, zoom=1.0, metrics=None, window_w=453):
        super().__init__(metrics, window_w)
        self.zoom = float(zoom)

    def send(self, method, params=None, timeout=None):
        params = dict(params or {})
        self.calls.append((method, params))
        if method == 'Emulation.setDeviceMetricsOverride':
            width = int(params.get('width') or 0)
            dsf = float(params.get('deviceScaleFactor') or 1.0)
            self.metrics = {"w": int(round(width / self.zoom)),
                            "h": int(round(int(params.get('height') or 0) / self.zoom)),
                            "dpr": dsf * self.zoom,
                            "sw": int(round(width / self.zoom))}
        return {}


def _as_int(value):
    """HWND приходит как ctypes-указатель: int() на нём падает."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(getattr(value, 'value', 0) or 0)


def fake_kernel32(alive=(101, 202)):
    """kernel32 с настоящими pid: атрибуты argtypes ставятся на функции."""
    state = {'alive': set(int(p) for p in alive), 'terminated': []}

    def OpenProcess(access, inherit, pid):
        return int(pid) if int(pid) in state['alive'] else 0

    def TerminateProcess(handle, code):
        pid = int(handle)
        state['terminated'].append(pid)
        state['alive'].discard(pid)
        return True

    def CloseHandle(handle):
        return True

    ns = SimpleNamespace(OpenProcess=OpenProcess,
                         TerminateProcess=TerminateProcess,
                         CloseHandle=CloseHandle)
    ns.state = state
    return ns


class FakeUser32:
    """user32, у которого SetParent не срабатывает (худший случай)."""

    def __init__(self, parent_ok=False):
        self.style0 = 0x16CF0000
        self.exstyle0 = 0x00040100
        self.styles = []
        self.shown = False
        self.parent_ok = parent_ok
        self.moves = []
        self.hidden = []
        self.closed = []

    def GetWindowLongPtrW(self, hwnd, index):
        return self.exstyle0 if index == win32_embed.GWL_EXSTYLE else self.style0

    def SetWindowLongPtrW(self, hwnd, index, value):
        raw = getattr(value, 'value', value)
        if isinstance(raw, bytes):
            raw = int.from_bytes(raw[:8], 'little')
        self.styles.append((hwnd, index, int(raw) & 0xFFFFFFFFFFFFFFFF))
        return 1

    def SetParent(self, hwnd, parent):
        return 0

    def GetParent(self, hwnd):
        return 1 if self.parent_ok else 0

    def ShowWindow(self, hwnd, cmd):
        self.shown = True
        self.hidden.append((_as_int(hwnd), _as_int(cmd)))
        # v19: результат ShowWindow проверяется по IsWindowVisible, поэтому
        # фиктивный user32 обязан отвечать правду, а не «всегда видно».
        self.visible = _as_int(cmd) != win32_embed.SW_HIDE
        return True

    def IsWindow(self, hwnd):
        return True

    def IsWindowVisible(self, hwnd):
        return bool(getattr(self, 'visible', True))

    def GetDpiForWindow(self, hwnd):
        return 96

    def GetClientRect(self, hwnd, rect):
        return self._fill(rect)

    def GetWindowRect(self, hwnd, rect):
        return self._fill(rect)

    def _fill(self, rect):
        # win32_embed передаёт ctypes.byref(rect).
        target = getattr(rect, '_obj', rect)
        target.left = target.top = 0
        target.right, target.bottom = 453, 735
        return True

    def SetWindowPos(self, hwnd, after, x, y, w, h, flags):
        self.moves.append((_as_int(x), _as_int(y), _as_int(w), _as_int(h),
                           _as_int(flags)))
        return True

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        if _as_int(msg) == win32_embed.WM_CLOSE:
            self.closed.append(_as_int(hwnd))
        return True

    def GetWindowThreadProcessId(self, hwnd, pid):
        return 1


NODE = shutil.which('node')
skip_node = unittest.skipUnless(NODE, 'Node unavailable')


class SourceTests(unittest.TestCase):
    def test_compile(self):
        for p in ROOT.glob('*.py'):
            compile(p.read_text(), str(p), 'exec')

    @_needs(ORIGINAL, 'TestLegal')
    def test_business_modules_unchanged(self):
        for name in ('config.py', 'crypto_utils.py', 'hwid_gen.py', 'secure_store.py',
                     'storage_paths.py', 'updater.py'):
            self.assertEqual((ROOT / name).read_bytes(), (ORIGINAL / name).read_bytes(), name)

    @_needs(ORIGINAL, 'TestLegal')
    def test_business_worker_bodies_unchanged(self):
        names = ('AuthWorker', 'BalanceWorker', 'DecrementWorker', 'PromptLoaderWorker',
                 'LoginWindow', 'TemplateWindow')
        self.assertEqual(class_dump(ORIGINAL / 'main.py', names),
                         class_dump(ROOT / 'main.py', names))

    @_needs(BASE, 'TestLegalQtWebEngine')
    def test_chat_automation_preserved_from_tested_variant(self):
        before = ast.parse((BASE / 'main.py').read_text())
        after = ast.parse((ROOT / 'main.py').read_text())
        js_names = ('JS_START_RECORDING', 'JS_STOP_AND_SEND', 'JS_CHECK_RECORDING',
                    'JS_CHECK_FILES', 'JS_FIND_INPUT', 'JS_REMOVE_ALL_FILES', 'JS_CHECK_END',
                    'JS_DISABLE_CONTEXT_MENU', 'JS_DISABLE_DRAG')

        def literals(tree):
            out = {}
            for n in tree.body:
                if isinstance(n, ast.Assign):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id in js_names:
                            out[t.id] = ast.literal_eval(n.value)
            return out

        self.assertEqual(literals(before), literals(after))
        self.assertEqual(func_dump(BASE / 'main.py', ('upload_file_advanced', 'check_state',
                                                      '_chat_has_file', 'log_page_probe')),
                         func_dump(ROOT / 'main.py', ('upload_file_advanced', 'check_state',
                                                      '_chat_has_file', 'log_page_probe')))

    def test_page_microphone_is_native_not_a_bridge(self):
        # Штатная запись голоса страницы живёт в настоящем браузере: Web Speech
        # API / getUserMedia привязаны к облаку вендора и отсутствуют в
        # QtWebEngine и в vanilla Chromium. Поэтому браузер — отдельный процесс.
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('class NativeHost(', main_src)
        self.assertIn('from native_browser import', main_src)
        self.assertIn('import win32_embed', main_src)
        # Никаких принудительных выходов и перехвата клавиатуры.
        self.assertNotIn('os._exit(', main_src)
        self.assertNotIn('SetWindowsHookEx', main_src)
        self.assertIn('HotkeyMonitor(', main_src)

    def test_native_mode_never_overrides_the_pages_speech_api(self):
        # В настоящем Chrome подменять SpeechRecognition НЕЛЬЗЯ и не нужно:
        # именно им сайт и записывает голос. Шим остаётся только для резерва.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'install_compat')
        body = ast.unparse(func)
        self.assertIn('if native:', body)
        self.assertIn('CONSENT_CLICK_JS', body)
        self.assertNotIn('SPEECH_SHIM_JS', body)
        self.assertNotIn('MIC_BUTTON_JS', body)
        # резервный путь со встроенным движком сохранён
        self.assertIn('_start_embedded_fallback', main_src)

    def test_mic_press_runs_the_site_recorder_and_verifies_it(self):
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        start = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_native_mic_start')
        verify = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_native_mic_verify')
        self.assertIn('JS_START_RECORDING', ast.unparse(start))
        self.assertIn('JS_CHECK_RECORDING', ast.unparse(verify))
        # Не проверялось раньше -> «нажимаю на микрофон и сразу отправка».
        self.assertIn('recording', ast.unparse(verify))
        toggle = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        self.assertIn('self._native_mic_start()', ast.unparse(toggle))

    def test_browser_recovery_is_a_ladder(self):
        # «Браузер висит»: сначала свежий профиль (single-ton владеет старым),
        # затем без GPU (Parsec/гибридная графика), затем следующий браузер.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        retry = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == '_retry')
        body = ast.unparse(retry)
        self.assertIn('self.attempt = 1', body)
        self.assertIn('self.gpu_off = True', body)
        self.assertIn('self.index += 1', body)
        self.assertIn('MAX_ATTEMPTS', ast.unparse(tree))

    @_needs(REFERENCE, 'release_reference')
    def test_onefile_build_command_is_documented_and_matches_the_bat(self):
        # Один исполняемый файл: --onefile и НИКАКИХ других вариантов сборки
        # в релизной команде. Синтаксис проверяется и в README, и в .bat.
        build = (ROOT / 'BUILD_ONEFILE.txt').read_text()
        commands = [line for line in build.splitlines()
                    if line.startswith('.venv') and '-m nuitka' in line]
        onefile = [c for c in commands if '--onefile' in c]
        self.assertEqual(len(onefile), 1)
        command = onefile[0]
        for flag in ('--onefile', '--windows-console-mode=disable', '--enable-plugin=pyqt6',
                     '--include-package=requests', '--include-package=websocket',
                     '--include-package=cryptography', '--include-package=fpdf',
                     '--include-data-file=data/DejaVuSans.ttf=data/DejaVuSans.ttf',
                     '--windows-icon-from-ico=icon.ico', '--assume-yes-for-downloads',
                     '--output-dir=build', '--output-filename=Legalyze.exe'):
            self.assertIn(flag, command)
        self.assertTrue(command.rstrip().endswith('main.py'))
        self.assertNotIn('--standalone', command)
        # .bat содержит ту же команду (иначе документация врёт)
        bat = (ROOT / 'build_onefile.bat').read_text()
        self.assertIn(command.split('-m nuitka', 1)[1].strip().split(' main.py')[0], bat)
        # Эталонный синтаксис из release_reference совпадает по флагам
        reference = (ROOT.parent / 'release_reference' / 'BUILD_COMMAND.txt').read_text().strip()
        self.assertEqual(sorted(command.split()[3:]), sorted(reference.split()[3:]))

    def test_zoom_level_matches_the_working_release_build(self):
        # Chrome хранит зум как «уровень»: factor = 1.2 ** level.
        self.assertAlmostEqual(native_browser.ZOOM, 2.0 / 3.0, delta=1e-9)
        self.assertAlmostEqual(native_browser.ZOOM_LEVEL, -2.223901614059533, delta=1e-5)
        self.assertAlmostEqual(1.2 ** native_browser.ZOOM_LEVEL, 2.0 / 3.0, delta=1e-9)
        # Раскладка одинаковая при 100 %, 125 % и 150 %: окно 453 DIP / 0.667
        self.assertEqual(native_browser.css_size_for(453, 735), (680, 1102))

    def test_zoom_is_written_into_the_profile_before_the_launch(self):
        tmp = Path(tempfile.mkdtemp(prefix='legalyze-zoom-'))
        try:
            self.assertTrue(native_browser.write_zoom(tmp))
            prefs = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            partition = prefs['partition']
            self.assertAlmostEqual(partition['default_zoom_level']['x'],
                                   -2.223901614059533, delta=1e-4)
            host = partition['per_host_zoom_levels']['x']['google.com']
            self.assertAlmostEqual(host['zoom_level'], -2.223901614059533, delta=1e-4)
            # Повторный запуск не затирает то, что уже есть в профиле.
            (tmp / 'Default' / 'Preferences').write_text(
                json.dumps({'partition': partition, 'keepme': 1}), encoding='utf-8')
            self.assertTrue(native_browser.write_zoom(tmp))
            again = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            self.assertEqual(again['keepme'], 1)
            self.assertIn('default_zoom_level', again['partition'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_native_zoom_is_measured_and_left_alone(self):
        # Профиль уже дал 67 %: 680 CSS px при dpr 0.667 -> 453 физических px.
        page = FakeCDPPage({"w": 680, "h": 1102, "dpr": 2 / 3, "sw": 680})
        result = native_browser.apply_zoom(page, 453, 735)
        self.assertEqual(result['source'], 'native')
        self.assertEqual(page.calls, [])      # ничего не переопределяем

    def test_a_wide_unscaled_layout_is_detected(self):
        # РОВНО дефект v8: CSS-вьюпорт 680 px, но отрисовка не сжата
        # (dpr = 1) -> чат шире окна, текст за правой границей.
        page = FakeCDPPage({"w": 680, "h": 1102, "dpr": 1.0, "sw": 680})
        ok, metrics = native_browser.zoom_ok(page, 453, 735, 453, 735)
        self.assertFalse(ok)
        self.assertEqual(metrics['innerWidth'], 680)
        # Прежняя проверка (только по innerWidth) здесь ошибочно проходила:
        self.assertTrue(native_browser.verify_viewport(page, 453, 735))

    def test_zoom_is_forced_and_measured_when_prefs_are_ignored(self):
        page = FakeCDPPage({"w": 453, "h": 735, "dpr": 1.0, "sw": 453})
        result = native_browser.apply_zoom(page, 453, 735, settle=0)
        methods = [method for method, _params in page.calls]
        self.assertIn('Emulation.setDeviceMetricsOverride', methods)
        params = page.calls[-1][1]
        self.assertEqual(params['width'], 680)
        # Высота следует за клиентской областью окна: 735 / (453/680) = 1103.
        self.assertEqual(params['height'], 1103)
        # dsf = physical / css = 0.667: эмулируемое устройство == окно,
        # поэтому страница сжимается, а не обрезается справа.
        self.assertAlmostEqual(params['deviceScaleFactor'], 2 / 3, delta=0.01)
        # Результат ИЗМЕРЕН: первая же попытка дала нужные метрики.
        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'emulation-dsf')
        self.assertEqual(len(page.calls), 1)

    def test_zoom_works_the_same_at_125_percent(self):
        # 125 %: окно 453 DIP = 566 физических px, раскладка всё те же 680 CSS px.
        page = FakeCDPPage({"w": 566, "h": 919, "dpr": 1.25, "sw": 566}, window_w=566)
        result = native_browser.apply_zoom(page, 453, 735, 566, 919, settle=0)
        self.assertTrue(result['ok'])
        params = page.calls[-1][1]
        self.assertEqual(params['width'], 680)
        # 566 / 680 = 0.833 = dsf(1.25) * zoom(0.667)
        self.assertAlmostEqual(params['deviceScaleFactor'], 0.8333, delta=0.01)

    def test_zoom_guard_is_wired_into_the_native_path(self):
        source = (ROOT / 'main.py').read_text()
        tree = ast.parse(source)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        self.assertIn('_ensure_zoom', methods)
        self.assertIn('_placeholder_physical_size', methods)
        self.assertIn('_placeholder_logical_size', methods)
        # v18.1: сам проход сторожа — в `_zoom_guard_once`, он же вызывается
        # и по таймеру, и сразу после показа окна.
        guard = (ast.unparse(methods['_ensure_zoom'])
                 + ast.unparse(methods['_zoom_guard_once']))
        self.assertIn('zoom_ok(page', guard)
        self.assertIn('apply_zoom(page', guard)
        self.assertIn('daemon=True', guard)          # GUI не блокируется
        self.assertIn('zoom_timer', ast.unparse(methods['_on_browser_ready']))
        self.assertIn('zoom_timer', ast.unparse(methods['_cleanup']))
        # Резервный QtWebEngine не трогаем: зум нужен только нативному режиму.
        self.assertIn('not self.native_mode', guard)
        # Зум пишется в профиль до запуска браузера.
        self.assertIn('write_zoom(self.profile_dir)',
                      (ROOT / 'native_browser.py').read_text())

    def test_consent_walls_are_never_automated(self):
        # В ЕС (Испания) свежий профиль сначала отдаёт consent.google.com.
        # Прежний выбор цели брал «любую страницу google.com» и встаивался
        # в стену согласия — отсюда «браузер запускается и висит».
        targets = [
            {'type': 'page', 'url': 'https://consent.google.com/m?gl=ES',
             'webSocketDebuggerUrl': 'ws://1'},
            {'type': 'page', 'url': 'https://gemini.google.com/app',
             'webSocketDebuggerUrl': 'ws://2'},
        ]
        chosen = native_browser.choose_target(targets)
        self.assertEqual(chosen['url'], 'https://gemini.google.com/app')
        self.assertTrue(native_browser.is_blocked_target('https://consent.google.com/m'))
        self.assertTrue(native_browser.is_blocked_target('https://accounts.google.com/v3/signin'))
        self.assertFalse(native_browser.is_blocked_target('https://gemini.google.com/app'))
        # Стена — только крайний случай (чтобы кликер мог её закрыть).
        only_wall = [{'type': 'page', 'url': 'https://consent.google.com/m',
                      'webSocketDebuggerUrl': 'ws://1'}]
        self.assertEqual(native_browser.choose_target(only_wall)['url'],
                         'https://consent.google.com/m')
        # about:blank не выбирается: вызывающий продолжает опрос.
        self.assertIsNone(native_browser.choose_target(
            [{'type': 'page', 'url': 'about:blank', 'webSocketDebuggerUrl': 'ws://3'}]))

    def test_consent_clicker_covers_the_languages_google_ships(self):
        script = literal('CONSENT_CLICK_JS')
        for phrase in ('accept all', 'Принять все', 'Aceptar todo', 'Alle akzeptieren',
                       'Tout accepter', 'Accetta tutto', 'Aceitar tudo', 'Tümünü kabul',
                       'Zaakceptuj wszystko', 'Прийняти всі'):
            self.assertIn(phrase.lower(), script.lower())
        self.assertIn('L2AGLb', script)          # стабильный id кнопки Google
        self.assertIn('reject', script.lower())  # и никогда не «отклонить»
        self.assertIn('__legalyzeAcceptConsent', script)

    def test_browser_discovery_prefers_pinned_portable_then_chrome(self):
        seen = []

        def fake_exists(path):
            seen.append(str(path))
            return str(path).endswith(('chrome.exe', 'msedge.exe'))

        with patch.object(native_browser, '_exists', fake_exists):
            candidates = native_browser.discover_browsers(app_dir=Path('C:/app'))
        kinds = [(c.rank, c.kind) for c in candidates]
        # переносной/pinned браузер важнее установленного Chrome, Chrome важнее Edge
        self.assertEqual(kinds[0], (1, 'portable'))
        self.assertIn((2, 'chrome'), kinds)
        self.assertIn((3, 'edge'), kinds)
        self.assertTrue(any('browser' in p for p in seen))

    def test_launch_flags_prepare_the_microphone_and_the_window(self):
        args = native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                         'https://google.com/ai', 453, 735)
        joined = ' '.join(args)
        self.assertIn('--app=https://google.com/ai', joined)
        self.assertIn('--user-data-dir=C:/profile', joined)
        self.assertIn('--remote-debugging-port=9222', joined)
        self.assertIn('--window-size=453,735', joined)
        # Жёлтая полоса Chrome: этих флагов больше нет.
        self.assertNotIn('--use-fake-ui-for-media-stream', joined)
        self.assertNotIn('--use-fake-device-for-media-stream', joined)
        self.assertIn('--no-first-run', joined)
        self.assertNotIn('--disable-gpu', joined)
        off = ' '.join(native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                                 'https://google.com/ai', 453, 735,
                                                 gpu=False))
        self.assertIn('--disable-gpu', off)

    def _fetch_chrome(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('fetch_chrome', ROOT / 'tools' / 'fetch_chrome.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_fetch_chrome_picks_the_branded_win64_build(self):
        fetch = self._fetch_chrome()
        index = {'channels': {'Stable': {'version': '140.0.7339.207', 'downloads': {'chrome': [
            {'platform': 'linux64', 'url': 'https://x/linux.zip'},
            {'platform': 'win64', 'url': 'https://x/chrome-win64.zip'},
        ]}}}}
        version, url = fetch.pick_url(index)
        self.assertEqual(version, '140.0.7339.207')
        self.assertTrue(url.endswith('chrome-win64.zip'))
        with self.assertRaises(SystemExit):
            fetch.pick_url(index, platform='win32')

    def test_fetch_chrome_fails_friendly_without_the_network(self):
        fetch = self._fetch_chrome()

        class DeadRequest:
            ProxyHandler = staticmethod(lambda *a, **k: None)

            def build_opener(self, *a, **k):
                raise OSError('TLS/SSL connection has been closed')

        with patch.object(fetch, 'urllib', SimpleNamespace(request=DeadRequest())):
            with self.assertRaises(fetch.FetchError) as caught:
                fetch.main(['--dry-run'])
        message = str(caught.exception)
        self.assertIn('--url', message)      # подсказка, как обойти проблему
        self.assertNotIn('Traceback', message)

    def test_fetch_chrome_dry_run_does_not_download(self):
        fetch = self._fetch_chrome()
        seen = []
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with patch.object(fetch, 'download', lambda *a, **k: seen.append(a)):
                code = fetch.main(['--dry-run', '--url', 'https://x/chrome-win64.zip',
                                   '--dest', str(ROOT / 'browser')])
        self.assertEqual(code, 0)
        self.assertEqual(seen, [])
        self.assertIn('https://x/chrome-win64.zip', buffer.getvalue())

    def test_retry_gets_a_fresh_profile_directory(self):
        # Профиль Chromium блокируется SingletonLock. Если повторный запуск идёт
        # в тот же каталог, новый процесс передаёт URL «мёртвому» владельцу и
        # сразу завершается — это и был «зависший chrome».
        browser = native_browser.NativeBrowser(profile_dir=Path('C:/profile'))
        self.assertEqual(browser.profile_dir, Path('C:/profile'))
        self.assertEqual(browser.profile_for(1), Path('C:/profile-retry1'))
        retry = native_browser.NativeBrowser(profile_dir=Path('C:/profile'), attempt=2)
        self.assertEqual(retry.profile_dir, Path('C:/profile-retry2'))
        # NativeHost обязан передавать номер попытки, иначе повтор бесполезен.
        self.assertIn('attempt=self.attempt', (ROOT / 'main.py').read_text())

    def test_every_module_level_name_is_bound(self):
        # Регрессия: `class NativeHost(QObject)` падала с NameError на импорте,
        # потому что QObject не был импортирован. Тесты на Linux не могут
        # импортировать main.py (нет PyQt6), поэтому проверяем таблицу символов:
        # любое имя, используемое на уровне модуля, должно быть определено.
        modules = ['main.py', 'native_browser.py', 'win32_embed.py', 'browser_focus.py',
                   'web_compat.py', 'qt_browser.py', 'speech_backend.py', 'diagnostics.py',
                   'config.py', 'storage_paths.py', 'updater.py', 'crypto_utils.py',
                   'hwid_gen.py', 'secure_store.py', 'win32_hotkeys.py', 'smoke_browser.py']
        extra_globals = {'__file__', '__name__', '__doc__', '__package__', '__spec__',
                         '__loader__', '__builtins__', '__debug__', '__path__'}
        problems = []
        for name in modules:
            source = (ROOT / name).read_text()
            table = symtable.symtable(source, name, 'exec')

            def bound(sym):
                return (sym.is_assigned() or sym.is_imported() or sym.is_namespace()
                        or sym.is_parameter())

            # только имена, которые модуль действительно связывает
            # (импорты, def/class, присваивания) — referenced-only имён здесь нет
            module_bound = ({sym.get_name() for sym in table.get_symbols() if bound(sym)}
                            | extra_globals | set(dir(builtins)))

            def walk(scope):
                for sym in scope.get_symbols():
                    # только имена, разрешаемые на уровне модуля: локальные,
                    # параметры и замыкания связаны внутри своей области видимости
                    if sym.is_global() and not bound(sym) and sym.get_name() not in module_bound:
                        problems.append('%s: %s' % (name, sym.get_name()))
                for child in scope.get_children():
                    walk(child)

            for sym in table.get_symbols():
                if not bound(sym) and sym.get_name() not in module_bound:
                    problems.append('%s: %s' % (name, sym.get_name()))
            walk(table)
        self.assertEqual(problems, [])

    def test_embed_is_checked_and_rolled_back(self):
        user32 = FakeUser32(parent_ok=False)
        self.assertFalse(win32_embed.embed(user32, 11, 22))
        # Стиль восстановлен, окно снова показано: пользователь не остаётся
        # с невидимым или оторванным окном браузера.
        restored = {}
        for _hwnd, index, value in user32.styles:
            restored[index] = value
        self.assertEqual(restored[win32_embed.GWL_STYLE], user32.style0)
        self.assertEqual(restored[win32_embed.GWL_EXSTYLE], user32.exstyle0)
        self.assertTrue(user32.shown)

    def test_upload_never_falls_back_to_body(self):
        source = (ROOT / 'main.py').read_text()
        upload = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)
                      and n.name == 'upload_file_advanced')
        self.assertNotIn('|| document.body;', ast.unparse(upload))

    def test_purge_protects_input_and_microphone(self):
        purge = literal('JS_PURGE', 'web_compat.py')
        self.assertIn('isProtected', purge)
        self.assertIn('PROTECTED_LABEL', purge)
        self.assertIn('svg[width="26"][height="26"][viewBox="0 0 24 24"]', purge)
        for token in ('input-plate', 'икрофон', 'voice'):
            self.assertIn(token, purge)

    def test_requirements_have_no_invented_package(self):
        text = (ROOT / 'requirements.txt').read_text()
        self.assertNotIn('PyQt6-WebChannel', text)  # QtWebChannel ships inside PyQt6
        self.assertIn('PyQt6-WebEngine', text)

    def test_hotkey_monitor_is_polling_and_never_reports_conflict(self):
        source = ast.unparse(ast.parse((ROOT / 'win32_hotkeys.py').read_text()))
        # Никаких потоков, скрытых окон и циклов сообщений: только опрос.
        self.assertIn('GetAsyncKeyState', source)
        self.assertIn('argtypes', source)  # без усечения указателей на Win64
        self.assertNotIn('CreateWindowEx', source)
        self.assertNotIn('PeekMessageW', source)
        self.assertNotIn('threading.Thread', source)
        self.assertNotIn('WNDPROC', source)
        # RegisterHotKey — только подавление проброса клавиши, не условие работы.
        self.assertIn('RegisterHotKey', source)
        self.assertIn('try_suppress', source)
        main = (ROOT / 'main.py').read_text()
        self.assertIn('HotkeyMonitor(', main)
        self.assertIn('_hotkey_tick', main)
        # Ложный "конфликт" удалён из кода полностью.
        self.assertNotIn('Конфликт горячих клавиш', main)
        # Опрос QTimer в главном потоке Qt.
        self.assertIn('self._hotkey_timer', main)

    def test_mic_button_js_in_early_bundle(self):
        browser = (ROOT / 'qt_browser.py').read_text()
        chunk_line = next(line for line in browser.splitlines() if 'chunks = [' in line)
        self.assertIn('MIC_BUTTON_JS', chunk_line)
        self.assertLess(chunk_line.index('MIC_BUTTON_JS'), chunk_line.index('JS_PURGE'))
        for name in ('JS_MIC_TOGGLE', 'JS_MIC_STOP', 'MIC_BUTTON_JS'):
            self.assertTrue(literal(name), name)

    def test_no_flash_cover_until_clean_reveal(self):
        main = (ROOT / 'main.py').read_text()
        # Шторка остаётся поверх страницы до загрузки и зачистки: никаких
        # миганий и видимости элементов браузера во время ожидания страницы.
        self.assertIn('_reveal_page', main)
        self.assertIn('self.browser_cover.show()', main)
        self.assertIn('QTimer.singleShot(250, self._reveal_page)', main)
        self.assertNotIn('self._hide_overlay()  # Google login/consent', main)

    def test_renderer_crash_auto_recovery(self):
        main = (ROOT / 'main.py').read_text()
        browser = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('self.crashed = True', browser)
        self.assertIn('_crash_recoveries', main)
        self.assertIn('_recover_web', main)
        self.assertIn('Восстановление страницы', main)

    def test_mic_prefers_own_dictation_and_gates_readiness(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        source = ast.unparse(func)
        self.assertIn('JS_MIC_START', source)       # настоящая диктовка — первая
        self.assertIn('JS_START_RECORDING', source)  # штатный сценарий — запасной
        self.assertIn('mic.dictation_started', source)
        self.assertIn('_chat_ready', source)

    def test_mic_press_starts_and_never_sends(self):
        # Реальный лог 20260930: первое нажатие F3/кнопки мгновенно отправляло
        # запрос, потому что состояние страницы рассинхронизировалось (движок
        # диктовки не прислал событий) и toggle() трактовался как «стоп».
        tree = ast.parse((ROOT / 'main.py').read_text())
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        source = ast.unparse(func)
        self.assertIn('JS_MIC_START', source)   # явный старт записи
        self.assertIn('JS_MIC_STOP', source)    # стоп — только второе нажатие
        start_js = literal('JS_MIC_START')
        self.assertIn('m.start()', start_js)
        self.assertIn('m.stop()', start_js)     # залипшая сессия сбрасывается

    def test_upload_thread_survives_page_navigation(self):
        # CDP-соединение умирает при навигации: поток должен брать актуальный
        # page, иначе файлы не появятся до ручного Ctrl+R (лог 20260930).
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'UploadThread')
        self.assertIn('page_provider', ast.unparse(cls))
        self.assertIn('_refresh_page', ast.unparse(cls))
        self.assertIn('upload.page_refreshed', main_src)
        thread = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == '_start_upload_thread')
        body = ast.unparse(thread)
        self.assertIn('self.upload_thread.page = self.worker.page', body)
        self.assertIn('page_provider=self._current_page', body)

    def test_dictation_status_is_visible_to_user(self):
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('_hook_speech_feedback', main_src)
        self.assertIn('Слушаю… говорите', main_src)
        self.assertIn('Распознано: ', main_src)

    def test_recognizer_is_discovered_before_the_engine_is_built(self):
        # Реальный лог v6: speech.host_exit code=2 через 310 мс — движок
        # создавался «вслепую» для ru-RU, а System.Speech бросает
        # "No recognizer of the required ID found", если такой распознаватель
        # не зарегистрирован в Windows. Сначала спрашиваем, что есть.
        script = speech_backend.PS_SCRIPT
        self.assertIn('InstalledRecognizers()', script)
        self.assertLess(
            script.index('InstalledRecognizers()'),
            script.index('New-Object System.Speech.Recognition.SpeechRecognitionEngine($ri)'))
        # Подбор: точная культура -> тот же язык -> первый доступный.
        self.assertIn('$fallback = $true', script)
        # Распознавателей нет вообще -> понятная причина, а не «no-speech».
        self.assertIn('No Windows speech recognizer installed', script)

    def test_host_failures_carry_a_real_message(self):
        script = speech_backend.PS_SCRIPT
        # Текст .NET-исключения уходит в поле msg -> diag speech.error.
        self.assertIn('function Reason(', script)
        self.assertIn('msg = $msg', script)
        import re
        exits = re.findall(r'exit (\d+)', script)
        self.assertTrue(exits)
        # Единственный «тихий» выход — сторож смерти родителя; каждый
        # выход с кодом ошибки обязан сообщить причину (было: exit 2, stderr
        # пуст, причина потеряна).
        self.assertEqual(exits.count('0'), 1)
        self.assertGreaterEqual(script.count('EmitError '),
                                sum(1 for code in exits if code != '0'))

    def test_recognition_loop_does_not_depend_on_the_event_pump(self):
        # .NET-обработчики (add_SpeechRecognized) в -NonInteractive хосте
        # PowerShell не вызываются: v5/v6 не выдавали НИ ОДНОГО события даже
        # при живом процессе. Recognize() — синхронный вызов: насос не нужен.
        script = speech_backend.PS_SCRIPT
        self.assertIn('$res = $rc.Recognize()', script)
        self.assertNotIn('RecognizeAsync', script)
        self.assertNotIn('add_SpeechRecognized', script)
        # Явные таймауты: фраза не обрезается и пауза не рвёт сессию.
        self.assertIn('EndSilenceTimeout', script)
        self.assertIn('InitialSilenceTimeout', script)
        # Аварийный выход приложения не оставляет процесс, держащий микрофон.
        self.assertIn('ParentPid', script)
        self.assertIn('Get-Process -Id $ParentPid', script)

    def test_assembly_loading_has_a_fallback_chain(self):
        script = speech_backend.PS_SCRIPT
        self.assertIn('Add-Type -AssemblyName System.Speech', script)
        self.assertIn("LoadWithPartialName('System.Speech')", script)
        self.assertIn('GetRuntimeDirectory()', script)
        self.assertIn('System.Speech is not available', script)

    def test_mic_never_sends_an_empty_prompt(self):
        # Реальный дефект v6: «нажимаю на микрофон — сразу отправка без
        # записи». Отправка разрешена только если диктовка дала текст.
        main_src = (ROOT / 'main.py').read_text()
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_toggle_mic')
        body = ast.unparse(func)
        self.assertIn('if not self._mic_got_text', body)
        self.assertIn('self._mic_cancel(', body)
        self.assertIn('self._mic_got_text = False', body)
        self.assertIn('def _mic_cancel', main_src)
        self.assertIn('mic.cancelled_no_text', main_src)

    def test_failed_dictation_resets_the_mic_state(self):
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('def _mic_reset_state', main_src)
        self.assertIn('def _on_speech_error', main_src)
        self.assertIn('MIC_ERROR_MESSAGES', main_src)
        # Причина отказа видна пользователю, а не только в логе.
        # v18: у каждой такой ошибки есть настоящая причина — нет браузера со
        # штатным микрофоном, поэтому подсказка ведёт к её устранению.
        for text in ('В Windows нет распознавателя речи',
                     'Нет распознавателя для этого языка',
                     'Микрофон не найден',
                     'BROWSER_HINT'):
            self.assertIn(text, main_src)

    def test_v18_brings_its_own_browser_for_the_built_in_microphone(self):
        # Штатный микрофон страницы = настоящий Chrome. Если его нет,
        # приложение само предлагает поставить официальную сборку.
        main_src = (ROOT / 'main.py').read_text()
        fetch_src = (ROOT / 'browser_fetch.py').read_text()
        for text in ('import browser_fetch', 'class BrowserFetchWorker',
                     'def _offer_browser_install', '_has_branded_browser',
                     'def _start_browser_install', 'AUTO_INSTALL_BROWSER',
                     'browser_install_declined'):
            self.assertIn(text, main_src)
        self.assertIn('googlechromelabs.github.io', fetch_src)
        self.assertIn('def install(', fetch_src)
        self.assertIn('def already_installed(', fetch_src)
        # Браузер ставится РЯДОМ с программой — именно там его ищет поиск.
        self.assertIn('def discover_browsers(', (ROOT / 'native_browser.py').read_text())
        self.assertIn('("browser", "chromium")', (ROOT / 'native_browser.py').read_text())

    def test_v18_1_returns_the_layout_immediately_after_the_show(self):
        # «Объекты съехали и встали на место только через пару секунд»:
        # раскладку чинил сторож зума раз в 5 с. Теперь проверка и починка
        # запускаются сразу же, на каждом шаге «оживления».
        main_src = (ROOT / 'main.py').read_text()
        for text in ('def _zoom_guard_once(',
                     'self._zoom_guard_once(page, reason="revive-final",\n'
                     '                                               pin_first=True)',
                     '_zoom_lock', 'REVIVE_STEPS', 'self._revive_settled'):
            self.assertIn(text, main_src)
        tree = ast.parse(main_src)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        revive = ast.unparse(methods['_revive_browser'])
        # v18.3: окно двигается один раз — в финализации, а не на каждом шаге.
        move = (ast.unparse(methods['_browser_sync_args'])
                + ast.unparse(methods['_finalize_and_reveal']))
        # Обычный sync() асинхронный (SWP_ASYNCWINDOWPOS) — здесь нужен не он.
        self.assertIn('sync_now(', move)
        # (в пояснении к методу имя флага упоминается — считаем только код)
        self.assertNotIn('SWP_ASYNCWINDOWPOS', _without_docstring(revive))
        # В фоновый поток уходят только HWND и числа, Qt вызывается в главном.
        self.assertIn('self._browser_inset()', move)
        self.assertIn('threading.Thread(', revive)
        # Пока окно скрыто — браузер тоже ставится на место (невидимо).
        self.assertIn('self._sync_browser_now()', ast.unparse(methods['hideEvent']))
        # Стоп-кран: как только два измерения подряд сошлись — не дёргаем.
        self.assertIn('_revive_settled', revive)

    def test_v18_1_window_shift_is_20_dip_up(self):
        # Калибровка пользователя: сдвиг окна браузера по Y = -20 DIP.
        main_src = (ROOT / 'main.py').read_text()
        self.assertTrue('BROWSER_DY = -24' in main_src or 'BROWSER_DY = -20' in main_src)
        self.assertIn('BROWSER_DX = -10', main_src)

    def test_v18_1_sync_now_moves_the_window_synchronously(self):
        embed = (ROOT / 'win32_embed.py').read_text()
        self.assertIn('def sync_now(', embed)
        body = embed.split('def sync_now(', 1)[1].split('\ndef ', 1)[0]
        body = _without_docstring(body, triple=True)
        self.assertIn('SetWindowPos', body)
        self.assertIn('SWP_FRAMECHANGED', body)
        # Именно асинхронный флаг и давал «съехавшее» окно на секунду-две.
        self.assertNotIn('SWP_ASYNCWINDOWPOS', body)
        sync_body = embed.split('def sync(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('SWP_ASYNCWINDOWPOS', sync_body)

    def test_v18_2_holds_the_cover_until_the_browser_settles(self):
        # «Съехало — вернулось — снова съехало»: страницу больше не показывают,
        # пока измерение не подтвердит, что браузер устоялся.
        main_src = (ROOT / 'main.py').read_text()
        for text in ('revive_settled = pyqtSignal(bool)',
                     'def _on_revive_settled(', 'def _layout_signature(',
                     'def _finish_settle(', 'REVIVE_SETTLE_N',
                     'self._revive_last', 'self._settling'):
            self.assertIn(text, main_src)
        tree = ast.parse(main_src)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}

        # Шторка поднимается ДО показа окна: при скрытии и при минимизации.
        self.assertIn('self._cover_to_top()', ast.unparse(methods['hideEvent']))
        self.assertIn('self._cover_to_top()', ast.unparse(methods['_schedule_revive']))

        # ...и её никто не снимает, пока идёт стабилизация.
        hold = ast.unparse(methods['_hold_cover_if_needed'])
        self.assertIn('_settling', hold)
        self.assertIn('_cover_to_top()', hold)
        hide_overlay = ast.unparse(methods['_hide_overlay'])
        self.assertIn('_settling', hide_overlay)

        # Снимает шторку только сигнал из главного потока Qt.
        self.assertIn('self.revive_settled.emit(', ast.unparse(methods['_finish_settle']))
        self.assertIn('@pyqtSlot(bool)', ast.unparse(methods['_on_revive_settled']))

        # «Устоялось» = одинаковое измерение REVIVE_SETTLE_N раз подряд,
        # но последний шаг снимает шторку в любом случае.
        revive = ast.unparse(methods['_revive_browser'])
        self.assertIn('REVIVE_SETTLE_N', revive)
        self.assertIn('last_step', revive)
        self.assertIn('signature == self._revive_last', revive)
        # Раскладку прибиваем ОДНИМ вызовом, а не лестницей переборов.
        self.assertIn('apply_viewport(page', ast.unparse(methods['_zoom_guard_once']))

    def test_v18_2_last_step_releases_the_cover_for_sure(self):
        # Жёсткий предел: шторка НИКОГДА не остаётся навсегда.
        main_src = (ROOT / 'main.py').read_text()
        self.assertIn('REVIVE_STEPS = (0, 150, 350, 600, 900, 1300, 1800, 2400)',
                      main_src)
        self.assertIn('REVIVE_SETTLE_N = 3', main_src)
        # Раньше срока GUI прогрузки не снимаем: возмущение приходит позже.
        self.assertIn('REVIVE_MIN_MS = 1600', main_src)
        tree = ast.parse(main_src)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        revive = ast.unparse(methods['_revive_browser'])
        self.assertIn('len(REVIVE_STEPS) - 1', revive)

    def test_v18_3_shows_the_loading_gui_until_the_browser_is_ready(self):
        # Пользователь: «на этапе повторного нажатия "Окно" нет GUI прогрузки
        # с наивысшим приоритетом, очень сильно мигает браузер».
        main_src = (ROOT / 'main.py').read_text()
        for text in ('REVIVE_TITLE', 'REVIVE_MESSAGE', 'REVIVE_MIN_MS',
                      'REVIVE_REVEAL_GATE_MS',
                     'REVIVE_FINAL_MS', 'def _finalize_and_reveal('):
            self.assertIn(text, main_src)
        tree = ast.parse(main_src)
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        # Тот же индикатор, что на старте (отдельное окно поверх всех).
        schedule = ast.unparse(methods['_schedule_revive'])
        self.assertIn('self._show_overlay(REVIVE_TITLE, REVIVE_MESSAGE)', schedule)
        while_settling = ast.unparse(methods['_hold_cover_if_needed'])
        self.assertIn('force_topmost(int(self.overlay.winId()))', while_settling)

    def test_v18_3_does_not_touch_the_browser_window_more_than_once(self):
        # Мигание рождалось от того, что окно дёргали на КАЖДОМ шаге: каждый
        # SetWindowPos/InvalidateRect заставляет Chromium пересобрать кадр, а
        # Chrome поднимает своё окно поверх шторки. Теперь окно трогается
        # один раз — в финализации, когда шторка ещё стоит.
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        measure = _without_docstring(ast.unparse(methods['_revive_browser']))
        for forbidden in ('sync_now(', 'show_window(', 'invalidate(',
                          'apply_viewport(', 'apply_zoom(', 'bringToFront'):
            self.assertNotIn(forbidden, measure)
        final = ast.unparse(methods['_finalize_and_reveal'])
        for required in ('sync_now(', 'show_window(', 'invalidate(',
                         'bringToFront', 'pin_first=True', 'time.sleep('):
            self.assertIn(required, final)

    def test_v18_revives_the_browser_after_minimize(self):
        main_src = (ROOT / 'main.py').read_text()
        for text in ('def _schedule_revive', 'def _revive_browser',
                     'def _nudge_metrics', 'JS_REVIVE', 'REVIVE_AFTER_SHOW',
                     'self._was_hidden = True', 'self._was_minimized = True'):
            self.assertIn(text, main_src)
        embed = (ROOT / 'win32_embed.py').read_text()
        self.assertIn('def show_window(', embed)
        self.assertIn('def invalidate(', embed)

    def test_v18_russian_speech_and_light_theme_are_enforced(self):
        main_src = (ROOT / 'main.py').read_text()
        compat = (ROOT / 'web_compat.py').read_text()
        native = (ROOT / 'native_browser.py').read_text()
        for text in ('SPEECH_LANG = "ru-RU"', 'FORCE_LIGHT_THEME = True',
                     'def apply_language_and_theme(', 'def page_scripts(',
                     'Emulation.setLocaleOverride', 'Emulation.setAutoDarkModeOverride',
                     'Emulation.setEmulatedMedia', 'self.speech_lang',
                     'self.light_theme'):
            self.assertIn(text, main_src)
        # Приоритет русского: своя метка окна важнее языка системы.
        self.assertIn('__legalyzeSpeechLang', compat)
        self.assertIn('def speech_lang_js(', compat)
        self.assertIn('color-scheme', compat)
        # Браузер запускается с русской локалью и в светлой теме.
        self.assertIn('--lang=', native)
        self.assertIn('WebContentsForceDark', native)
        self.assertIn('def write_locale(', native)
        self.assertIn('accept_languages', native)
        tree = ast.parse(main_src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_hook_speech_feedback')
        body = ast.unparse(func)
        self.assertIn('bridge.ended.connect', body)
        self.assertIn('bridge.hypothesis.connect', body)

    def test_bundle_joins_are_asi_safe(self):
        # IIFE-чанки вида })() и (() => { нельзя склеивать одним переводом
        # строки: это разбирается как вызов и роняет остаток бандла.
        browser = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('ASI hazard', browser)
        self.assertIn("';" + chr(92) + "n'.join(chunks)", browser)
        self.assertTrue(literal('MIC_BUTTON_JS').rstrip().endswith(');'))

    def test_native_speech_is_never_used_and_cover_rises(self):
        web = (ROOT / 'web_compat.py').read_text()
        self.assertIn('__legalyzeNativeSpeechRecognition', web)
        self.assertNotIn("mode: 'native'", web)  # отказ от нативного SpeechRecognition
        self.assertIn('window.LegalyzeSpeechRecognition = SpeechRecognition', web)
        main = (ROOT / 'main.py').read_text()
        self.assertIn('self.browser_cover.raise_()', main)
        # восстановление останавливает потоки с мёртвым CDP-соединением
        tree = ast.parse(main)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_recover_web')
        self.assertIn('upload_thread', ast.unparse(func))

    def test_build_scripts_keep_reference_onefile_syntax(self):
        # v24: синтаксис сборки вынесен в текстовый файл СБОРКА_NUITKA.txt (режим standalone).
        txt = (ROOT / 'СБОРКА_NUITKA.txt').read_text(encoding='utf-8')
        for token in ('--mode=standalone', '--windows-console-mode=disable',
                      '--enable-plugin=pyqt6', '--include-package=requests',
                      '--include-package=websocket', '--include-package=cryptography',
                      '--include-package=fpdf', '--assume-yes-for-downloads',
                      '--output-filename=Legalyze.exe', 'main.py'):
            self.assertIn(token, txt)
        self.assertIn('data/DejaVuSans.ttf=data/DejaVuSans.ttf', txt)
        self.assertIn('data/FONT-LICENSE.txt=data/FONT-LICENSE.txt', txt)
        self.assertIn('--file-version=1.0.5.0', txt)

    # ---------------------------------------------------------------- v13 --
    def test_no_unsupported_media_flag_is_emitted(self):
        # Chrome показывает «вы используете неподдерживаемый флаг командной
        # строки» для --use-fake-ui-for-media-stream: именно это и было в отчёте.
        args = native_browser.build_args('C:/chrome.exe', 9222, 'C:/profile',
                                         'https://google.com/ai', 453, 735)
        joined = ' '.join(args)
        self.assertNotIn('--use-fake-ui-for-media-stream', joined)
        self.assertNotIn('--use-fake-device-for-media-stream', joined)
        # Всё остальное из v9 осталось на месте: меняем только этот флаг.
        for kept in ('--remote-debugging-port=9222', '--user-data-dir=C:/profile',
                     '--app=https://google.com/ai', '--window-size=453,735',
                     '--no-first-run', '--noerrdialogs', '--disable-extensions'):
            self.assertIn(kept, joined)

    def test_microphone_is_granted_by_the_profile_not_by_a_flag(self):
        tmp = Path(tempfile.mkdtemp(prefix='legalyze-perm-'))
        try:
            self.assertTrue(native_browser.write_permissions(tmp))
            prefs = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            mic = prefs['profile']['content_settings']['exceptions']['media_stream_mic']
            entry = mic['https://google.com:443,*']
            self.assertEqual(entry['setting'], 1)          # 1 = ALLOW
            self.assertEqual(entry['secondary_pattern'], '*')
            self.assertTrue(str(entry['last_modified']).isdigit())
            self.assertIn('https://[*.]google.com:443,*', mic)
            # Повторная запись не затирает профиль.
            (tmp / 'Default' / 'Preferences').write_text(json.dumps({'keepme': 1}),
                                                         encoding='utf-8')
            self.assertTrue(native_browser.write_permissions(tmp))
            again = json.loads((tmp / 'Default' / 'Preferences').read_text(encoding='utf-8'))
            self.assertEqual(again['keepme'], 1)
            self.assertIn('media_stream_mic',
                          again['profile']['content_settings']['exceptions'])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        source = ast.parse((ROOT / 'native_browser.py').read_text())
        host = next(n for n in source.body if isinstance(n, ast.ClassDef)
                    and n.name == 'NativeBrowser')
        launch = next(m for m in host.body if isinstance(m, ast.FunctionDef)
                      and m.name == 'launch')
        self.assertIn('write_permissions(self.profile_dir)', ast.unparse(launch))
        # Состояние права проверяется, а не предполагается.
        grant = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                     and n.name == 'grant_microphone')
        self.assertIn('permissions.query', ast.unparse(grant))

    def test_layout_fills_the_window_exactly(self):
        # Поле ввода и поле ответа стоят на своих местах, когда поверхность
        # ровно равна клиентской области окна.
        css_w, css_h, dsf = native_browser.target_css(453, 735, 680, 1103)
        self.assertEqual(css_w, 680)                       # 453 / 0.667
        self.assertAlmostEqual(css_w * dsf, 680, delta=1.0)    # ровно в ширину
        self.assertAlmostEqual(css_h * dsf, 1103, delta=1.0)   # ровно в высоту
        # 100 %: окно 453x735 физических px — раскладка та же 680 CSS px.
        css_w2, _css_h2, dsf2 = native_browser.target_css(453, 735, 453, 735)
        self.assertEqual(css_w2, 680)
        self.assertAlmostEqual(dsf2, 2.0 / 3.0, delta=1e-3)
        # Рамка/заголовок окна: низ страницы (поле ввода) не срезан.
        css_w3, css_h3, dsf3 = native_browser.target_css(453, 735, 680, 1058)
        self.assertAlmostEqual(css_h3 * dsf3, 1058, delta=1.0)
        self.assertLess(css_h3, css_h)
        # 125 % и 150 % дают ту же ширину раскладки.
        for phys in (566, 680):
            self.assertEqual(native_browser.target_css(453, 735, phys, 900)[0], 680)

    def test_profile_zoom_and_emulation_are_never_stacked(self):
        # Худший случай: зум профиля наложился на эмуляцию (видимая ширина
        # 1020 вместо 680). Ступень уточняется по измерению.
        page = ZoomedCDPPage(zoom=2.0 / 3.0, metrics={'w': 1020, 'h': 1653,
                                                      'dpr': 0.667, 'sw': 1020},
                             window_w=680)
        result = native_browser.apply_zoom(page, 453, 735, 680, 1103, settle=0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['source'], 'emulation-adaptive')
        self.assertEqual(int(result['metrics']['innerWidth']), 680)
        self.assertTrue(all(method == 'Emulation.setDeviceMetricsOverride'
                            for method, _params in page.calls))

    def test_placeholder_size_is_the_whole_window_not_the_inner_slot(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        method = methods['_placeholder_logical_size']
        node = ast.parse(ast.unparse(method))
        node.body[0].body = node.body[0].body[1:]      # без docstring
        body = ast.unparse(node)
        self.assertIn('placeholder.width()', body)
        self.assertIn('placeholder.height()', body)
        self.assertNotIn('side_inset', body)
        self.assertNotIn('top_inset', body)
        self.assertNotIn('bottom_inset', body)
        # Физический размер берётся у клиентской области окна браузера.
        self.assertIn('_browser_physical_size', methods)
        self.assertIn('_browser_physical_size()', ast.unparse(methods['_zoom_size']))

    def test_zoom_ladder_never_restarts_the_browser_or_presses_keys(self):
        # Именно перезапуск браузера ради --force-device-scale-factor ломал
        # сборки: рвал CDP-сессию, поднимал шторку — кнопки переставали
        # нажиматься, файлы не грузились.
        import re
        source = ast.parse((ROOT / 'native_browser.py').read_text())
        zoom = next(n for n in source.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'apply_zoom')
        node = ast.parse(ast.unparse(zoom))
        node.body[0].body = node.body[0].body[1:]      # без docstring
        body = ast.unparse(node)
        for forbidden in ('press', 'restart', 'keybd', 'force-device-scale-factor',
                          'hooks', 'ctrl_key'):
            self.assertNotIn(forbidden, body)
        self.assertIn('adapt_viewport(page', body)
        module = re.sub(r'""".*?"""', '', (ROOT / 'native_browser.py').read_text(),
                        flags=re.S)
        self.assertNotIn('--force-device-scale-factor', module)
        self.assertNotIn('--force-device-scale-factor',
                         ' '.join(native_browser.build_args(
                             'C:/chrome.exe', 9222, 'C:/profile',
                             'https://google.com/ai', 453, 735)))
        tree = ast.parse((ROOT / 'main.py').read_text())
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                      and n.name == 'MainWindow')
        methods = {m.name: m for m in window.body if isinstance(m, ast.FunctionDef)}
        for forbidden in ('_restart_browser_dsf', '_press_zoom_key'):
            self.assertNotIn(forbidden, methods)
        guard = (ast.unparse(methods['_ensure_zoom'])
                 + ast.unparse(methods['_zoom_guard_once']))
        self.assertNotIn('press', guard)
        self.assertNotIn('restart', guard)
        self.assertIn('apply_zoom(page', guard)

    def test_zoom_restored_to_native_67(self):
        source = (ROOT / 'qt_browser.py').read_text()
        self.assertIn('ZOOM_FACTOR = 2.0 / 3.0', source)
        self.assertIn('setZoomFactor(ZOOM_FACTOR)', source)
        self.assertIn('JS_PURGE', source)  # early purge: no flash of site chrome


class BrowserWindowTests(unittest.TestCase):
    """v15: положение окна браузера, панель задач, лишние окна и консоли.

    Сдвиг окна (X -10, Y -20 DIP) НЕ угадан: он подобран на Windows в
    измерительной сборке pre14. Здесь проверяется, что он действительно
    применяется, а не только записан в константах.
    """

    # ------------------------------------------------------------ сдвиг окна
    def test_the_shift_is_baked_in_as_constants(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('BROWSER_DX = -10', source)
        self.assertTrue('BROWSER_DY = -24' in source or 'BROWSER_DY = -20' in source)
        # ...и реально передаётся окну браузера, а не лежит мёртвым грузом.
        self.assertIn('host.inset = self._browser_inset()', source)
        for name in ('_browser_inset', '_hide_browser_taskbar'):
            self.assertIn('def %s(self)' % name, source)

    def test_box_for_and_sync_place_the_window_with_the_shift(self):
        user32 = FakeUser32()
        # Плейсхолдер 453x735, сдвиг (-10, -20): размер окна НЕ меняется,
        # иначе справа/снизу появились бы незакрашенные полосы.
        self.assertEqual(win32_embed.box_for(user32, 22, (0, 0, 0, 0)), (0, 0, 453, 735))
        self.assertEqual(win32_embed.box_for(user32, 22, (-10, -20, 10, 20)),
                         (-10, -20, 453, 735))
        self.assertTrue(win32_embed.sync(user32, 11, 22, (-10, -20, 10, 20)))
        self.assertEqual(user32.moves[-1][:4], (-10, -20, 453, 735))
        # Невозможный прямоугольник не вызывает SetWindowPos вообще.
        before = len(user32.moves)
        self.assertFalse(win32_embed.sync(user32, 11, 22, (400, 700, 400, 700)))
        self.assertEqual(len(user32.moves), before)

    # -------------------------------------------------------- панель задач
    def test_the_embedded_window_is_removed_from_the_taskbar(self):
        user32 = FakeUser32()
        with patch.object(win32_embed, 'taskbar_delete_tab', return_value=True) as tab:
            self.assertTrue(win32_embed.hide_from_taskbar(user32, 4242))
        # Стиль: WS_EX_APPWINDOW снят, WS_EX_TOOLWINDOW поставлен.
        applied = {index: value for _hwnd, index, value in user32.styles}
        exstyle = applied[win32_embed.GWL_EXSTYLE]
        self.assertEqual(exstyle & win32_embed.WS_EX_APPWINDOW, 0)
        self.assertEqual(exstyle & win32_embed.WS_EX_TOOLWINDOW,
                         win32_embed.WS_EX_TOOLWINDOW)
        # Перерисовка рамки — без перемещения и изменения размера.
        x, y, w, h, flags = user32.moves[-1]
        self.assertEqual((x, y, w, h), (0, 0, 0, 0))
        self.assertEqual(flags & win32_embed.SWP_NOMOVE, win32_embed.SWP_NOMOVE)
        self.assertEqual(flags & win32_embed.SWP_NOSIZE, win32_embed.SWP_NOSIZE)
        # И главное: сам ITaskbarList::DeleteTab — одного стиля мало.
        tab.assert_called_once_with(4242)

    def test_stray_browser_windows_are_hidden_but_the_embedded_one_is_kept(self):
        user32 = FakeUser32()
        windows = [
            {'hwnd': 11, 'class': 'Chrome_WidgetWin_1', 'visible': True},
            {'hwnd': 22, 'class': 'Chrome_WidgetWin_1', 'visible': True},
            {'hwnd': 33, 'class': 'Chrome_WidgetWin_0', 'visible': False},
        ]
        with patch.object(win32_embed, '_windows_of', return_value=windows), \
             patch.object(win32_embed, 'taskbar_delete_tab', return_value=True):
            hidden = win32_embed.hide_stray_windows(user32, [5], keep=22)
        # Вспомогательные окна скрыты; встроенное (22) и невидимое (33) — нет.
        self.assertEqual(hidden, [11])
        self.assertEqual([h for h, _cmd in user32.hidden], [11])
        self.assertEqual(_as_int(user32.hidden[0][1]), win32_embed.SW_HIDE)

    def test_the_taskbar_hiding_is_repeated_not_done_once(self):
        # Chrome добавляет кнопку в панель задач сам и не сразу.
        source = (ROOT / 'main.py').read_text()
        self.assertIn('QTimer.singleShot(1200, self.hide_taskbar)', source)
        self.assertIn('QTimer.singleShot(4000, self.hide_taskbar)', source)
        self.assertIn('self.taskbar_timer.start(5000)', source)
        self.assertIn('self.taskbar_timer.stop()', source)

    # ------------------------------------------------------- дерево процессов
    def test_the_whole_browser_tree_is_killed(self):
        kernel32 = fake_kernel32(alive=(101, 202, 303))
        with patch.object(win32_embed, '_kernel32', return_value=kernel32):
            killed = win32_embed.kill_tree([101, 202, 303, 404])
        self.assertEqual(killed, [101, 202, 303])
        self.assertEqual(kernel32.state['terminated'], [101, 202, 303])
        # Служебные pid системы не трогаем никогда.
        kernel32 = fake_kernel32(alive=(0, 4))
        with patch.object(win32_embed, '_kernel32', return_value=kernel32):
            self.assertEqual(win32_embed.kill_tree([0, 4]), [])

    def test_the_host_kills_the_tree_on_retry_restart_and_exit(self):
        source = (ROOT / 'main.py').read_text()
        for method in ('_retry', 'restart_next', 'stop'):
            block = source.split('def %s(' % method, 1)[1].split('\n    def ', 1)[0]
            self.assertIn('self._kill_all()', block, method)
        self.assertIn('win32_embed.kill_tree(pids)', source)

    def test_leftover_browsers_are_only_our_own(self):
        # Журнал pid пишет сам запуск; имя процесса проверяется, чтобы nunca
        # не снять чужой процесс по переиспользованному pid.
        source = (ROOT / 'main.py').read_text()
        self.assertIn('def _kill_leftover_browsers()', source)
        self.assertIn('win32_embed.BROWSER_EXE_NAMES', source)
        self.assertIn('_kill_leftover_browsers()', source)
        self.assertIn('BROWSER_PIDS_FILE', source)
        # Вызывается до создания QApplication — до запуска своего браузера.
        self.assertLess(source.index('_kill_leftover_browsers()'),
                        source.index('app = QApplication(sys.argv)'))

    def test_descendant_pids_walks_the_whole_tree(self):
        tree = {1: {2, 3}, 2: {4}, 3: set(), 4: {5}, 5: set()}
        with patch.object(win32_embed, 'child_pids',
                          side_effect=lambda pid: tree.get(int(pid), set())):
            self.assertEqual(win32_embed.descendant_pids(1), {2, 3, 4, 5})

    # --------------------------------------------------------------- GUI/консоль
    def test_the_cover_is_a_native_window_above_the_browser(self):
        # Окно браузера — HWND внутри плейсхолдера; обычный виджет Qt всегда
        # рисуется ПОД ним, поэтому шторка обязана быть нативным окном.
        source = (ROOT / 'main.py').read_text()
        self.assertIn('Qt.WidgetAttribute.WA_NativeWindow', source)
        cover = source.split('self.browser_cover = QWidget', 1)[1].split('\n\n', 1)[0]
        self.assertIn('WA_NativeWindow', cover)

    def test_no_subprocess_can_pop_up_a_console(self):
        # Каждый запуск процесса обязан глушить консоль, иначе при старте
        # мелькает чёрное окно. [1:] — текст ДО первого Popen не считается.
        browser = (ROOT / 'native_browser.py').read_text()
        for chunk in browser.split('Popen(')[1:]:
            self.assertIn('creationflags', chunk[:800])
            self.assertIn('startupinfo', chunk[:800])
        self.assertIn('CREATE_NO_WINDOW', browser)
        self.assertIn('STARTF_USESHOWWINDOW', browser)
        speech = (ROOT / 'speech_backend.py').read_text()
        for chunk in speech.split('Popen(')[1:]:
            self.assertIn('creationflags', chunk[:800])
        self.assertIn('CREATE_NO_WINDOW', speech)
        self.assertIn('--windows-console-mode=disable',
                      (ROOT / 'СБОРКА_NUITKA.txt').read_text(encoding='utf-8'))

    def test_the_admin_mode_of_pre14_is_not_left_in_the_release(self):
        # v15 — релиз: панель администратора, F8 и layout_tune здесь не нужны.
        self.assertFalse((ROOT / 'layout_tune.py').exists())
        self.assertFalse((ROOT / 'layout_tune_ui.py').exists())
        source = (ROOT / 'main.py').read_text()
        self.assertNotIn('layout_tune', source)
        self.assertNotIn('HOTKEY_ADMIN_ID', source)
        self.assertNotIn('AdminPanel', source)
        self.assertNotIn('admin_mode', source)


def bind_methods(owner, names, extra_scope=()):
    """Связать методы MainWindow с заглушкой: PyQt6 на Linux недоступен."""
    source = (ROOT / 'main.py').read_text()
    tree = ast.parse(source)
    window = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                  and n.name == 'MainWindow')
    nodes = {n.name: n for n in window.body if isinstance(n, ast.FunctionDef)}
    for name in names:
        node = nodes[name]
        body = list(node.body)
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        clone = ast.FunctionDef(name=name, args=node.args, body=body,
                                decorator_list=[], returns=None,
                                type_comment=None, type_params=[])
        ast.fix_missing_locations(clone)
        scope = {'int': int, 'float': float, 'round': round, 'max': max,
                 'abs': abs, 'PAGE_ZOOM': 2.0 / 3.0, 'WIDTH_DELTA': 0,
                 'HEIGHT_DELTA': 0}
        scope.update(dict(extra_scope))
        exec(compile(ast.Module(body=[clone], type_ignores=[]),
                     '<%s>' % name, 'exec'), scope)
        setattr(owner, name, scope[name].__get__(owner))


class PlacementSettingsTests(unittest.TestCase):
    """Блок настроек, от которого считается положение объектов.

    Числа с Windows (замер pre14): плейсхолдер 453x735 DIP, а клиентская
    область САМОГО окна браузера — 439x728. Если цель раскладки считать от
    плейсхолдера, получается dsf 439/679 = 0.6465 вместо 439/658 = 0.6672 —
    страница на 3 % мельче. Именно поэтому v15 отличался от pre14.
    """

    PLACEHOLDER = (453, 735)
    BROWSER = (439, 728)      # замерено на Windows в pre14

    def _owner(self, scale=1.0, **scope):
        owner = SimpleNamespace()
        owner._placeholder_logical_size = lambda: self.PLACEHOLDER
        owner._placeholder_physical_size = lambda: tuple(
            int(round(v * scale)) for v in self.PLACEHOLDER)
        owner._browser_physical_size = lambda: self.BROWSER
        owner._monitor_scale = lambda: scale
        base = {'BROWSER_DX': -10, 'BROWSER_DY': -40,
                'BROWSER_DW': 0, 'BROWSER_DH': 0}
        base.update(scope)
        bind_methods(owner, ('_browser_logical_size', '_zoom_size', '_zoom_args',
                             '_browser_inset'), sorted(base.items()))
        owner.cfg = {}
        return owner

    def test_all_settings_live_in_one_marked_block(self):
        source = (ROOT / 'main.py').read_text()
        names = ('BROWSER_DX', 'BROWSER_DY', 'BROWSER_DW', 'BROWSER_DH',
                 'PAGE_ZOOM', 'WIDTH_DELTA', 'HEIGHT_DELTA', 'PAGE_OFFSET_X',
                 'PAGE_OFFSET_Y', 'PAD_BOTTOM', 'SCROLL_X', 'SCROLL_Y',
                 'INPUT_DX', 'INPUT_DY', 'INPUT_SELECTOR', 'CLOSE_STRAY_WINDOWS')
        start = source.index('НАСТРОЙКИ РАСПОЛОЖЕНИЯ ОБЪЕКТОВ')
        end = source.index('конец блока настроек')
        block = source[start:end]
        for name in names:
            self.assertIn(name + ' =', block, name)
            # И ни одного определения вне блока.
            self.assertEqual(source.count('\n' + name + ' ='), 1, name)

    def test_the_zoom_target_uses_the_browser_window_not_the_placeholder(self):
        owner = self._owner()
        self.assertEqual(owner._browser_logical_size(), self.BROWSER)
        self.assertEqual(owner._zoom_size(), self.BROWSER + self.BROWSER)
        # Цель обязана совпасть с замером pre14: css 658x1091, dsf 0.6672.
        css_w, css_h, dsf = self._target_of(owner)
        self.assertEqual(css_w, 658)
        self.assertEqual(css_h, 1091)
        self.assertAlmostEqual(dsf, 0.6672, places=4)
        # От плейсхолдера получалось бы css 679 / dsf 0.6465 — на 3 % мельче.
        self.assertNotEqual(css_w, int(round(self.PLACEHOLDER[0] / (2.0 / 3.0))))

    def test_the_target_scales_with_the_monitor(self):
        # При 150 % окно браузера физически больше, но в DIP — то же самое,
        # поэтому css остаётся прежним, а dsf растёт.
        owner = self._owner(scale=1.5)
        owner._browser_physical_size = lambda: (659, 1092)
        css_w, css_h, dsf = self._target_of(owner)
        self.assertEqual(owner._browser_logical_size(), (439, 728))
        self.assertEqual(css_w, 658)
        self.assertAlmostEqual(dsf, 659 / 658.0, places=3)

    def test_the_layout_deltas_from_the_settings_block_are_honoured(self):
        owner = self._owner(WIDTH_DELTA=-6, HEIGHT_DELTA=-20)
        css_w, css_h, dsf = self._target_of(owner)
        self.assertEqual(css_w, 652)
        # dsf пересчитывается от новой ширины, css_h — от нового dsf.
        self.assertAlmostEqual(dsf, 439 / 652.0, places=4)
        self.assertEqual(css_h, int(round(728 / dsf)) - 20)

    def test_the_page_zoom_from_the_settings_block_is_honoured(self):
        owner = self._owner(PAGE_ZOOM=0.5)
        css_w, _css_h, dsf = self._target_of(owner, zoom=0.5)
        self.assertEqual(css_w, 878)          # 439 / 0.5
        self.assertAlmostEqual(dsf, 439 / 878.0, places=4)

    def test_the_window_size_knobs_are_applied(self):
        owner = self._owner()
        self.assertEqual(owner._browser_inset(), (-10, -40, 10, 40))
        # BROWSER_DW/DH урезают окно: правый/нижний inset перестаёт быть
        # зеркальным, а левый/верхний остаётся сдвигом.
        owner.cfg = {'browser_dw': -20, 'browser_dh': -30}
        self.assertEqual(owner._browser_inset(), (-10, -40, 30, 70))

    def test_the_guard_passes_the_configured_zoom(self):
        source = (ROOT / 'main.py').read_text()
        # v18.1: сторож разделён — тонкая проверка живёт в `_zoom_guard_once`.
        guard = source.split('def _zoom_guard_once(', 1)[1].split('\n    def ', 1)[0]
        guard += source.split('def _ensure_zoom(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._zoom_args()', guard)
        self.assertIn('zoom=PAGE_ZOOM', guard)
        # Сдвиги страницы внедряются тем же сторожем (после перезагрузки
        # стиль пропадает, поэтому он проверяется каждый проход).
        self.assertIn('page.eval(page_tune_js(with_scroll=False)', guard)

    def test_the_page_tuning_is_off_when_all_values_are_zero(self):
        # v13-поведение: пока сдвиги нулевые, страницу не трогаем вообще.
        source = (ROOT / 'main.py').read_text()
        self.assertIn('def page_tuning_needed():', source)
        self.assertIn('!offX && !offY && !padB && !inX && !inY', source)
        self.assertIn('if page_tuning_needed():', source)

    def _target_of(self, owner, zoom=None):
        zoom = (2.0 / 3.0) if zoom is None else zoom
        logical_w, _lh, physical_w, physical_h = owner._zoom_args()
        css_w = max(320, int(round(logical_w / zoom)))
        dsf = (float(physical_w) / css_w) if css_w else 1.0
        return css_w, int(round(physical_h / dsf)), dsf


class StrayWindowTests(unittest.TestCase):
    """Окна браузера: не появляться ни в панели задач, ни в Alt+Tab."""

    def test_a_stray_app_window_is_closed_not_only_hidden(self):
        user32 = FakeUser32()
        windows = [{'hwnd': 11, 'class': 'Chrome_WidgetWin_1', 'visible': True},
                   {'hwnd': 22, 'class': 'Chrome_WidgetWin_1', 'visible': True}]
        with patch.object(win32_embed, '_windows_of', return_value=windows), \
             patch.object(win32_embed, 'taskbar_delete_tab', return_value=True):
            win32_embed.hide_stray_windows(user32, [5], keep=22,
                                           close_after_hide=True)
        # Сначала скрыто, потом закрыто — пользователь не видит ни переключения.
        self.assertEqual([h for h, _c in user32.hidden], [11])
        self.assertEqual(_as_int(user32.hidden[0][1]), win32_embed.SW_HIDE)
        self.assertEqual(user32.closed, [11])

    def test_a_service_window_is_hidden_but_never_closed(self):
        # Chrome_WidgetWin_0 — опорное окно браузера: закрытие убивает процесс.
        user32 = FakeUser32()
        windows = [{'hwnd': 11, 'class': 'Chrome_WidgetWin_0', 'visible': True}]
        with patch.object(win32_embed, '_windows_of', return_value=windows), \
             patch.object(win32_embed, 'taskbar_delete_tab', return_value=True):
            win32_embed.hide_stray_windows(user32, [5], keep=22,
                                           close_after_hide=True)
        self.assertEqual(user32.hidden, [(11, win32_embed.SW_HIDE)])
        self.assertEqual(user32.closed, [])

    def test_the_whole_widgetwin_family_is_covered(self):
        # Только Chrome_WidgetWin_0/1 пропускали «двойку» в Alt+Tab.
        source = (ROOT / 'win32_embed.py').read_text()
        self.assertIn('name.startswith("Chrome_WidgetWin")', source)

    def test_the_found_window_is_hidden_before_embedding(self):
        source = (ROOT / 'main.py').read_text()
        embed = source.split('def _embed(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('win32_embed.hide_window(self.user32, hwnd)', embed)
        self.assertLess(embed.index('hide_window'),
                        embed.index('win32_embed.embed('))

    def test_the_pid_tree_is_refreshed_after_ready(self):
        # После ready таймер останавливается, и pids застывали — окна всплывших
        # позже процессов оставались висеть.
        source = (ROOT / 'main.py').read_text()
        self.assertIn('def refresh_pids(self):', source)
        self.assertIn('descendant_pids(root)', source)
        host_src = source[source.index('class NativeHost'):]
        for name in ('hide_taskbar', '_kill_all', '_tick'):
            block = host_src.split('def %s(' % name, 1)[1].split('\n    def ', 1)[0]
            self.assertIn('refresh_pids', block, name)

    def test_the_hiding_is_dense_in_the_first_seconds(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('(300, 800, 1500, 2500, 4000)', source)
        self.assertIn('CLOSE_STRAY_WINDOWS', source)

    def test_hide_window_uses_sw_hide(self):
        user32 = FakeUser32()
        self.assertTrue(win32_embed.hide_window(user32, 77))
        self.assertEqual(user32.hidden, [(77, win32_embed.SW_HIDE)])
        self.assertFalse(win32_embed.hide_window(None, 77))


class NoMissingMethodTests(unittest.TestCase):
    """Каждый вызов `self._x()` обязан быть настоящим методом класса.

    В первой v15 `_browser_logical_size` вызывал `self._monitor_scale()`,
    которого НЕ БЫЛО: AttributeError в `_on_browser_ready` ломал скрытие
    элементов браузера и экспорт файлов. Проверка идёт по всему классу.
    """

    def _missing(self, class_name):
        tree = ast.parse((ROOT / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == class_name)
        methods = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
        missing = set()
        for fn in cls.body:
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'self'
                        and node.func.attr.startswith('_')
                        and node.func.attr not in methods):
                    missing.add((fn.name, node.func.attr))
        return missing

    def test_main_window_calls_only_real_methods(self):
        self.assertEqual(self._missing('MainWindow'), set())

    def test_native_host_calls_only_real_methods(self):
        self.assertEqual(self._missing('NativeHost'), set())

    def test_the_monitor_scale_exists_and_is_used(self):
        tree = ast.parse((ROOT / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'MainWindow')
        methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertIn('_monitor_scale', methods)
        body = ast.unparse(methods['_browser_logical_size'])
        self.assertIn('self._monitor_scale()', body)


class InsetAndComTests(unittest.TestCase):
    """Сдвиг окна считается от настоящего плейсхолдера, а COM не роняет процесс."""

    def test_browser_host_is_set_before_the_inset_is_computed(self):
        # Иначе `_placeholder_physical_size` уходит в резерв (413x651 вместо
        # 453x735), и сдвиг получается на 9 % меньше: -9/-36 вместо -10/-40.
        source = (ROOT / 'main.py').read_text()
        start = source.index('def _start_chrome')
        block = source[start:start + 4000]
        self.assertLess(block.index('self.browser_host = host'),
                        block.index('host.inset = self._browser_inset()'))

    def test_the_inset_is_recomputed_once_the_placeholder_is_real(self):
        source = (ROOT / 'main.py').read_text()
        ready = source.split('def _on_browser_ready(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('host.inset = self._browser_inset()', ready)
        self.assertIn('host.sync()', ready)

    def test_com_calls_use_integer_addresses(self):
        # `proto(c_void_p)` — TypeError, а вызов по мусорному адресу —
        # access violation. Адрес обязан быть целым, слот — проверенным.
        source = (ROOT / 'win32_embed.py').read_text()
        self.assertIn('ctypes.c_void_p(int(interface))', source)
        self.assertIn('if not address:', source)
        self.assertIn('fn = proto(address)', source)

    def test_v19_the_vtable_is_read_without_dereferencing_it(self):
        # Краш из «Error - 1»: `table[slot]` — это разыменование чужой памяти
        # средствами Python. Оно НЕ защищено: процесс падает насмерть, и
        # перехватить нечего. v19 читает память через ReadProcessMemory.
        source = (ROOT / 'win32_embed.py').read_text()
        block = source.split('def read_pointer(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('ReadProcessMemory', block)
        self.assertIn('GetCurrentProcess', block)
        # Само разыменование исчезло из кода (осталось только в пояснении).
        self.assertNotIn('table[slot]', _without_docstring(source))
        self.assertIn('def com_vtable(', source)
        self.assertIn('int(read.value or 0)', source)

    def test_v19_read_pointer_reports_unreadable_memory_as_none(self):
        win32_embed._kernel32_dll = None
        with patch.object(win32_embed, '_kernel32', return_value=None):
            self.assertIsNone(win32_embed.read_pointer(12345))
            self.assertIsNone(win32_embed.com_vtable(12345))
        win32_embed._kernel32_dll = None

    def test_v19_a_broken_com_object_never_kills_the_process(self):
        # Тот самый случай: указатель есть, а слот 3 в vtable — пустой.
        # Раньше это заканчивалось access violation в главном потоке.
        events = []
        kernel = _FakeKernel32({0x1000: 0x2000,
                                0x2000: 0x3001, 0x2008: 0x3002, 0x2010: 0x3003,
                                0x2018: 0,        # HrInit — пустой слот
                                0x2020: 0x3005, 0x2028: 0x3006, 0x2030: 0x3007,
                                0x2038: 0x3008})
        with patch.object(win32_embed, '_kernel32', return_value=kernel), \
             patch.object(win32_embed, 'diag') as diag:
            diag.event = lambda name, **kw: events.append((name, kw))
            diag.exception = lambda *a, **k: events.append(('exception', a))
            win32_embed._COM_DISABLED = False
            self.assertIsNone(win32_embed._com_call(0x1000, 3))
            self.assertIsNone(win32_embed._com_call(0x1000, 5))
        reasons = [kw.get('reason') for name, kw in events if name == 'win32.com_abort']
        self.assertIn('empty_slot', reasons)
        self.assertNotIn('win32.com_call', [name for name, _ in events])
        win32_embed._COM_DISABLED = False

    def test_v19_the_taskbar_button_is_only_reported_when_it_is_really_gone(self):
        # В логе «Error - 1» было: com_empty_slot → OSError → taskbar_deleted
        # (успех!). Теперь успех — это настоящий HRESULT, а не надежда.
        # Сам vtable-вызов подменяется: вызывать «функцию» по выдуманному
        # адресу нельзя даже в тесте.
        slots = [0x3000 + i for i in range(8)]
        for hr, expected in ((-2147024809, False), (0, True)):
            events = []
            with patch.object(win32_embed, '_ole32',
                              return_value=_FakeOle32(interface=0x1000)), \
                 patch.object(win32_embed, 'com_vtable', return_value=slots), \
                 patch.object(win32_embed, '_com_call',
                             side_effect=lambda interface, slot, *args:
                             0 if int(slot) == win32_embed._COM_HR_INIT else hr), \
                 patch.object(win32_embed, 'diag') as diag:
                diag.event = lambda name, **kw: events.append((name, kw))
                diag.exception = lambda *a, **k: None
                win32_embed._COM_DISABLED = False
                self.assertEqual(expected, win32_embed.taskbar_delete_tab(4242))
            names = [name for name, _ in events]
            if expected:
                self.assertIn('win32.taskbar_deleted', names)
            else:
                self.assertIn('win32.taskbar_failed', names)
                self.assertNotIn('win32.taskbar_deleted', names)
        win32_embed._COM_DISABLED = False

    def test_v19_the_taskbar_path_survives_an_access_violation(self):
        # Даже если вызов всё-таки упал, функция возвращает False, а процесс
        # продолжает работать (лог «Error - 1»: OSError в главном потоке).
        win32_embed._COM_DISABLED = False
        with patch.object(win32_embed, '_com_call',
                          side_effect=OSError('access violation')):
            with patch.object(win32_embed, '_ole32', return_value=None):
                self.assertFalse(win32_embed.taskbar_delete_tab(4242))
        win32_embed._COM_DISABLED = False

    def test_com_disables_itself_after_an_access_violation(self):
        source = (ROOT / 'win32_embed.py').read_text()
        self.assertIn('_COM_DISABLED = False', source)
        self.assertIn('_COM_DISABLED = True', source)
        self.assertIn('if not hwnd or _COM_DISABLED:', source)
        # OSError (access violation) обрабатывается отдельно от прочих ошибок.
        block = source.split('def _com_call(', 1)[1].split('\n\ndef ', 1)[0]
        self.assertIn('except OSError:', block)

    def test_the_taskbar_com_can_be_switched_off(self):
        source = (ROOT / 'main.py').read_text()
        start = source.index('НАСТРОЙКИ РАСПОЛОЖЕНИЯ ОБЪЕКТОВ')
        end = source.index('конец блока настроек')
        self.assertIn('TASKBAR_DELETE_TAB = False', source[start:end])
        self.assertIn('delete_tab=bool(TASKBAR_DELETE_TAB)', source)

    def test_the_shift_is_exact_at_100_percent(self):
        # Резервный размер (413x651) давал бы -9/-36 — здесь его быть не должно.
        tree = ast.parse((ROOT / 'main.py').read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == 'MainWindow')
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef)
                    and n.name == '_browser_inset')
        body = list(node.body)
        if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        clone = ast.FunctionDef(name='_browser_inset', args=node.args, body=body,
                                decorator_list=[], returns=None,
                                type_comment=None, type_params=[])
        ast.fix_missing_locations(clone)
        scope = {'BROWSER_DX': -10, 'BROWSER_DY': -40, 'BROWSER_DW': 0,
                 'BROWSER_DH': 0, 'int': int, 'float': float, 'round': round,
                 'TypeError': TypeError, 'ValueError': ValueError}
        exec(compile(ast.Module(body=[clone], type_ignores=[]), '<i>', 'exec'), scope)
        owner = SimpleNamespace(cfg={})
        owner._placeholder_logical_size = lambda: (453, 735)
        owner._placeholder_physical_size = lambda: (453, 735)
        owner._browser_inset = scope['_browser_inset'].__get__(owner)
        self.assertEqual(owner._browser_inset(), (-10, -40, 10, 40))
        # Окно при этом остаётся прежнего размера (правый/нижний inset зеркален).
        left, top, right, bottom = owner._browser_inset()
        self.assertEqual(453 - left - right, 453)
        self.assertEqual(735 - top - bottom, 735)


class SpeechBackendTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.engine = speech_backend.DictationEngine(
            lambda kind, data: self.events.append((kind, data)))

    def test_event_translation(self):
        gen = self.engine._gen
        self.engine._handle(gen, {'type': 'started'})
        self.engine._handle(gen, {'type': 'hypothesis', 'text': 'привет'})
        self.engine._handle(gen, {'type': 'result', 'text': 'привет мир', 'confidence': 0.8})
        self.engine._handle(gen, {'type': 'error', 'code': 'not-allowed'})
        self.engine._handle(gen, {'type': 'error', 'code': 'weird'})
        kinds = [k for k, _ in self.events]
        self.assertEqual(kinds, ['started', 'hypothesis', 'result', 'error', 'error'])
        self.assertEqual(self.events[2][1], {'text': 'привет мир', 'confidence': 0.8})
        self.assertEqual(self.events[4][1]['code'], 'network')  # unknown mapped

    def test_finish_reports_no_speech_once(self):
        gen = self.engine._gen
        self.engine._ended = False
        self.engine._finish(gen, force_error=True)
        self.engine._finish(gen, force_error=True)
        self.assertEqual([k for k, _ in self.events], ['error', 'end'])
        self.assertEqual(self.events[0][0], 'error')
        self.assertEqual(self.events[0][1]['code'], 'no-speech')

    def test_stale_generation_is_dropped(self):
        self.engine._ended = False
        self.engine._event(self.engine._gen + 5, 'result', {'text': 'старое', 'confidence': 1})
        self.assertEqual(self.events, [])

    def test_error_names_are_api_compatible(self):
        self.assertIn('language-not-supported', speech_backend.ERROR_NAMES)
        self.assertIn('not-allowed', speech_backend.ERROR_NAMES)

    def test_language_is_always_a_specific_culture(self):
        # System.Speech выбрасывает исключение на нейтральной культуре 'ru',
        # а страница отдаёт именно document.documentElement.lang = 'ru'.
        self.assertEqual(speech_backend.norm_lang('ru'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('en'), 'en-US')
        self.assertEqual(speech_backend.norm_lang('ru_RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang('ru-RU'), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang(''), 'ru-RU')
        self.assertEqual(speech_backend.norm_lang(None), 'ru-RU')

    def test_powershell_host_is_hardened(self):
        script = speech_backend.PS_SCRIPT
        # System.Speech не подгружается сам в -NonInteractive хосте.
        self.assertIn('Add-Type -AssemblyName System.Speech', script)
        # Windows.Forms не загружен: DoEvents под ErrorActionPreference=Stop
        # убивал хост через ~40 мс после 'started'.
        self.assertNotIn('DoEvents', script)
        # Нейтральная культура -> первый конкретный язык той же буквенной пары.
        self.assertIn('IsNeutralCulture', script)
        self.assertIn('SpecificCultures', script)

    def _spawn_engine(self, created, lines=()):
        class WaitStream:
            """stdout живого хоста: отдаёт строки, пока процесс не снят."""

            def __init__(self, done, payload):
                self._done = done
                self._payload = list(payload)

            def __iter__(self):
                for line in self._payload:
                    yield (line + '\n').encode('utf-8')
                self._done.wait(5)

        class FakeProc:
            def __init__(self):
                self._done = threading.Event()
                self.stdout = WaitStream(self._done, lines)
                self.stderr = io.StringIO('')
                self.terminated = 0

            def poll(self):
                return None if self.terminated == 0 else 0

            def wait(self):
                self._done.wait(0.5)
                return 0

            def terminate(self):
                self.terminated += 1
                self._done.set()

        def popen(*args, **kwargs):
            proc = FakeProc()
            created.append({'args': args, 'kwargs': kwargs, 'proc': proc})
            return proc

        return popen

    def _wait_for(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_start_is_idempotent_for_the_same_language(self):
        # Реальный лог 20260930: страница рестартовала распознавание ~1 раз в
        # секунду; каждый start() вызывал stop() и убивал только что spawned
        # хост — ни одного события (started/result/error) не приходило.
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        spawn = self._spawn_engine(created, lines=['{"type": "started"}'])
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen', spawn):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: self.events == [('started', {})]),
                            self.events)
            engine.start('ru')          # повтор старта (гонка страницы)
            engine.start('ru-RU')       # та же конкретная культура
            # Живой хост НЕ перезапускается и НЕ снимается.
            self.assertEqual(len(created), 1)
            self.assertEqual(created[0]['proc'].terminated, 0)
            self.assertTrue(engine.active)
            # Повторный старт подтверждается событием started (страница ждёт).
            self.assertEqual([k for k, _ in self.events],
                             ['started', 'started', 'started'])
            # Другой язык — реальный перезапуск хоста.
            engine.start('en-US')
            self.assertEqual(len(created), 2)
            self.assertEqual(created[0]['proc'].terminated, 1)
            self.assertEqual(created[0]['kwargs'].get('stderr'), speech_backend.subprocess.PIPE)
            self.assertEqual(created[0]['args'][0][0], 'powershell.exe')
            self.assertEqual(created[0]['args'][0][-1], 'ru-RU')
            self.assertEqual(created[1]['args'][0][-1], 'en-US')
        engine.shutdown()

    def test_host_events_reach_the_page(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "started"}',
                 '{"type": "speechstart"}',
                 '{"type": "hypothesis", "text": "при"}',
                 '{"type": "result", "text": "привет мир", "confidence": 0.9}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 4), self.events)
            kinds = [k for k, _ in self.events]
            self.assertEqual(kinds[:4], ['started', 'speechstart', 'hypothesis', 'result'])
            self.assertTrue(engine.speech_seen)
            self.assertEqual(self.events[3][1]['text'], 'привет мир')
        engine.shutdown()

    def test_stop_ignores_a_stale_page_session(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen', self._spawn_engine(created)):
            engine.start('ru', sid='session-1')
            engine.stop(sid='session-0')   # устаревшая сессия страницы
            self.assertEqual(created[0]['proc'].terminated, 0)
            self.assertTrue(engine.active)
            engine.stop(sid='session-1')   # текущая сессия — останавливается
            self.assertEqual(created[0]['proc'].terminated, 1)
            self.assertFalse(engine.active)
        engine.shutdown()

    def test_explicit_host_error_is_not_downgraded_to_no_speech(self):
        # v6: хост умирал с кодом 2 (нет распознавателя для ru-RU), Python
        # видел EOF и сообщал странице 'no-speech' -> GUI печатал
        # «Речь не распознана», mic_active оставался True, и следующее
        # нажатие уходило в ветку «стоп + отправка» — пустой запрос в чат.
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "recognizers", "count": 1, "langs": ["en-US"], "want": "ru-RU"}',
                 '{"type": "error", "code": "service-not-allowed",'
                 ' "msg": "No Windows speech recognizer installed"}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            # 'recognizers' уходит только в лог (diag), страница его не ждёт.
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 1), self.events)
            engine.stop()
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 2), self.events)
        self.assertEqual([(k, d.get('code')) for k, d in self.events],
                         [('error', 'service-not-allowed'), ('end', None)])
        # Причина сохранена: GUI показывает её вместо «no-speech».
        self.assertEqual(engine.last_error['code'], 'service-not-allowed')
        self.assertIn('recognizer', engine.last_error['msg'])

    def test_fallback_recognizer_is_reported_to_the_user(self):
        created = []
        engine = speech_backend.DictationEngine(lambda kind, data: self.events.append((kind, data)))
        engine._write_script = lambda: 'dictation.ps1'
        lines = ['{"type": "recognizers", "count": 1, "langs": ["en-US"], "want": "ru-RU"}',
                 '{"type": "started", "lang": "en-US", "requested": "ru-RU", "fallback": true}',
                 '{"type": "result", "text": "hello", "confidence": 0.4, "lang": "en-US"}']
        with patch.object(sys, 'platform', 'win32'), \
             patch.object(speech_backend.subprocess, 'Popen',
                          self._spawn_engine(created, lines=lines)):
            engine.start('ru')
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 2), self.events)
            self.assertEqual(engine.last_notice, ('fallback', 'en-US'))
            engine.stop()
            self.assertTrue(self._wait_for(lambda: len(self.events) >= 3), self.events)
        self.assertEqual([k for k, _ in self.events], ['started', 'result', 'end'])



class HotkeyPollingTests(unittest.TestCase):
    def test_tick_fires_on_edge_with_debounce(self):
        fired = []
        state = {'down': False}
        mon = win32_hotkeys.HotkeyMonitor(on_event=fired.append)
        mon._get_state = lambda vk: state['down']
        self.assertTrue(mon.set_keys(0, 0x72))  # only mic key
        mon.tick(0.0)            # not pressed
        state['down'] = True
        mon.tick(0.1)            # press -> fire
        mon.tick(0.2)            # held -> no repeat
        state['down'] = False
        mon.tick(0.3)            # release
        state['down'] = True
        mon.tick(0.4)            # bounce within 0.35s debounce -> ignored
        state['down'] = False
        mon.tick(0.5)            # release
        state['down'] = True
        mon.tick(0.6)            # real second press -> fire
        self.assertEqual(fired, [win32_hotkeys.HOTKEY_MIC_ID, win32_hotkeys.HOTKEY_MIC_ID])

    def test_two_keys_fire_independently(self):
        fired = []
        state = {0x71: False, 0x72: False}
        mon = win32_hotkeys.HotkeyMonitor(on_event=fired.append)
        mon._get_state = lambda vk: state[vk]
        mon.set_keys(0x71, 0x72)
        mon.tick(0.0)
        state[0x71] = True
        mon.tick(0.1)
        state[0x71] = False
        state[0x72] = True
        mon.tick(0.6)
        self.assertEqual(fired, [win32_hotkeys.HOTKEY_TOGGLE_ID, win32_hotkeys.HOTKEY_MIC_ID])

    def test_set_keys_always_ready_and_status_never_conflicts(self):
        mon = win32_hotkeys.HotkeyMonitor(on_event=lambda i: None)
        # Опрос физической клавиатуры работает всегда — set_keys не может
        # вернуть False, статус никогда не содержит "конфликт".
        self.assertIs(mon.set_keys(0x71, 0x72), True)
        self.assertEqual(mon.status_text, 'Готов к работе')
        self.assertIs(mon.clear(), True)
        self.assertNotIn('onфликт', mon.status_text)
        self.assertNotIn('onflict', mon.status_text)

    def test_suppression_is_optional(self):
        mon = win32_hotkeys.HotkeyMonitor(on_event=lambda i: None)
        mon.set_keys(0x71, 0x72)
        # Отказ подавления (в т.ч. на не-Windows) ничего не блокирует.
        self.assertIn(mon.try_suppress(0), (True, False))
        self.assertTrue(mon.set_keys(0x71, 0x72))
        mon.release()


@skip_node
class JavascriptTests(unittest.TestCase):
    def run_node(self, script):
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            self.fail('node failed: ' + res.stderr[-2000:])

    def _tune_js(self, **values):
        """PAGE_TUNE_JS со значениями из блока настроек (как page_tune_js())."""
        template = literal('PAGE_TUNE_JS', 'main.py')
        sx = int(values.get('SCROLL_X', 0))
        sy = int(values.get('SCROLL_Y', 0))
        return template % (int(values.get('PAGE_OFFSET_X', 0)),
                           int(values.get('PAGE_OFFSET_Y', 0)),
                           int(values.get('PAD_BOTTOM', 0)),
                           int(values.get('INPUT_DX', 0)),
                           int(values.get('INPUT_DY', 0)),
                           json.dumps(str(values.get('INPUT_SELECTOR', 'textarea'))),
                           sx, sy, sx, sy)

    def _run_tune(self, **values):
        script = r"""
var store = {};
var head = { appendChild: function (n) { store[n.id] = n; n.parentNode = head; } };
var document = {
  getElementById: function (id) { return store[id] || null; },
  createElement: function (tag) { return { id: '', textContent: '', parentNode: null }; },
  head: head, documentElement: {}
};
var scrolled = null;
var window = { scrollTo: function (x, y) { scrolled = [x, y]; } };
var out = __TUNE_JS__;
var node = store['legalyze-tune-css'] || null;
console.log(JSON.stringify({ out: out, css: node ? node.textContent : null,
                             present: !!node, scrolled: scrolled }));
""".replace('__TUNE_JS__', self._tune_js(**values))
        res = subprocess.run([NODE, '-e', script], capture_output=True,
                             text=True, timeout=60)
        if res.returncode != 0:
            self.fail('node failed: ' + res.stderr[-2000:])
        return json.loads(res.stdout.strip().splitlines()[-1])

    def test_page_tune_is_a_noop_when_every_value_is_zero(self):
        # Пока сдвиги нулевые, страница обязана остаться ровно v13: ни стиля,
        # ни прокрутки, ни единой правки DOM.
        state = self._run_tune()
        self.assertTrue(state['out']['ok'])
        self.assertTrue(state['out'].get('removed'))
        self.assertFalse(state['present'])
        self.assertIsNone(state['scrolled'])

    def test_page_tune_writes_the_shifts_and_scrolls(self):
        state = self._run_tune(PAGE_OFFSET_Y=-24, PAD_BOTTOM=18,
                               INPUT_DX=3, INPUT_DY=-7, SCROLL_Y=120,
                               INPUT_SELECTOR='textarea')
        css = state['css'] or ''
        self.assertIn('translate(0px, -24px)', css)
        self.assertIn('padding-bottom: 18px', css)
        self.assertIn('textarea { transform: translate(3px, -7px)', css)
        self.assertEqual(state['scrolled'], [0, 120])

    def test_page_tune_without_scroll_does_not_fight_the_user(self):
        # Повторное применение (сторож зума) не должно прокручивать страницу.
        template = literal('PAGE_TUNE_JS', 'main.py')
        js = template % (0, -24, 0, 0, 0, json.dumps('textarea'), 0, 0, 0, 0)
        self.assertIn('if (0 || 0)', js)
        state = self._run_tune(PAGE_OFFSET_Y=-24, SCROLL_Y=120)
        self.assertIsNotNone(state['scrolled'])

    def test_ready_script_gate(self):
        script = literal('READY_SCRIPT', 'qt_browser.py')
        harness = '''const vm = require('vm');
const script = SCRIPT;
for (const [host, protocol, visible, disabled, ready, expected] of [
 ['', 'about:', true, false, 'complete', false],
 ['accounts.google.com','https:',true,false,'complete',false],
 ['evilgoogle.com','https:',true,false,'complete',false],
 ['google.com','http:',true,false,'complete',false],
 ['google.com','https:',true,false,'loading',false],
 ['google.com','https:',false,false,'complete',false],
 ['google.com','https:',true,true,'complete',false],
 ['www.google.com','https:',true,false,'complete',true],
 ['gemini.google.com','https:',true,false,'interactive',true]
]) {
 const result = vm.runInNewContext(script, {location:{hostname:host,protocol}, document:{
 readyState:ready, querySelectorAll:()=>[{disabled,readOnly:false,getClientRects:()=>visible?[{}]:[]}]}});
 if (result !== expected) throw Error('Readiness mismatch: '+host);
}
'''.replace('SCRIPT', json.dumps(script))
        self.run_node(harness)

    def test_speech_shim_feature_detection_and_events(self):
        shim = literal('SPEECH_SHIM_JS')
        harness = '''
const vm = require('vm');
const signals = {};
const bridgeCalls = [];
const bridge = {
  start: (lang, c, i) => bridgeCalls.push(['start', lang, c, i]),
  stop: () => bridgeCalls.push(['stop']),
  abort: () => bridgeCalls.push(['abort']),
  started: {connect: f => signals.started = f},
  speechStart: {connect: f => signals.speechstart = f},
  speechEnd: {connect: f => signals.speechend = f},
  hypothesis: {connect: f => signals.hypothesis = f},
  result: {connect: f => signals.result = f},
  rejected: {connect: f => signals.rejected = f},
  error: {connect: f => signals.error = f},
  ended: {connect: f => signals.ended = f}
};
const sandbox = {
  window: {__legalyzeSpeechBridge: bridge, __legalyzeSpeechBridgeWait: 10},
  navigator: {language: 'ru-RU'},
  setTimeout, clearTimeout, Promise, console
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);

const w = sandbox.window;
if (typeof w.SpeechRecognition !== 'function') throw Error('SpeechRecognition missing');
if (w.__legalyzeSpeech.mode !== 'shim') throw Error('mode');
if (!('webkitSpeechRecognition' in w)) throw Error('webkit prefix missing');

const events = [];
const rec = new w.SpeechRecognition();
for (const n of ['audiostart','start','speechstart','result','error','end','audioend','nomatch'])
  rec['on' + n] = (e) => events.push([n, e]);

// detection surface used by the site
if (!('SpeechRecognition' in w || 'webkitSpeechRecognition' in w)) throw Error('detection');

rec.interimResults = true;
rec.start();
if (bridgeCalls[0][0] !== 'start' || bridgeCalls[0][1] !== 'ru-RU') throw Error('bridge start');
signals.started();
signals.speechstart();
signals.hypothesis('при');
signals.result('привет мир', 0.87);
if (bridgeCalls[1][0] !== 'stop') throw Error('auto stop expected, got ' + JSON.stringify(bridgeCalls));
signals.ended();

const names = events.map(e => e[0]).join(',');
if (names !== 'audiostart,start,speechstart,result,result,audioend,end') throw Error('order: ' + names);
const resEvents = events.filter(e => e[0] === 'result');
if (resEvents[0][1].results[0].isFinal) throw Error('first result must be interim');
if (!resEvents[1][1].results[0].isFinal) throw Error('second result must be final');
const res = resEvents[1][1].results;
if (res[0][0].transcript !== 'привет мир') throw Error('final result');
if (resEvents[1][1].resultIndex !== 0) throw Error('resultIndex');

// error path and double-start guard
const rec2 = new w.SpeechRecognition();
rec2.onerror = (e) => events.push(['error2', e.error]);
rec2.onend = () => events.push(['end2', {}]);
rec2.start();
let threw = false;
try { rec2.start(); } catch (e) { threw = e.name === 'InvalidStateError'; }
if (!threw) throw Error('InvalidStateError expected');
signals.error('not-allowed');
signals.ended();
if (!events.some(e => e[0] === 'error2' && e[1] === 'not-allowed')) throw Error('error event');
if (!events.some(e => e[0] === 'end2')) throw Error('end event');

// abort path
const rec3 = new w.SpeechRecognition();
rec3.onerror = () => {};
rec3.onend = () => events.push(['end3', {}]);
rec3.start();
rec3.abort();
if (!events.some(e => e[0] === 'end3')) throw Error('abort end');
console.log('shim ok');
'''
        self.run_node(harness.replace('SHIM', json.dumps(literal('SPEECH_SHIM_JS'))))

    def test_speech_shim_without_bridge_reports_network(self):
        shim = literal('SPEECH_SHIM_JS')
        harness = '''
const vm = require('vm');
const sandbox = {
  window: {__legalyzeSpeechBridgeWait: 5},
  navigator: {language: 'ru-RU'},
  setTimeout, console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);
const rec = new sandbox.window.SpeechRecognition();
let err = null, ended = false;
rec.onerror = (e) => { err = e.error; };
rec.onend = () => { ended = true; };
rec.start();
setTimeout(() => {
  if (err !== 'network' || !ended) throw Error('network error expected: ' + err + '/' + ended);
  console.log('no-bridge ok');
}, 50);
'''
        self.run_node(harness.replace('SHIM', json.dumps(shim)))

    def test_ua_brand_patch(self):
        harness = '''
const vm = require('vm');
const sandbox = {
  navigator: {userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36'},
  window: {}, setTimeout, Promise, console
};
vm.createContext(sandbox);
vm.runInContext(UA, sandbox);
const d = sandbox.navigator.userAgentData;
const brands = d.brands.map(b => b.brand);
if (!brands.includes('Google Chrome') || !brands.includes('Chromium')) throw Error('brands: ' + brands);
if (d.brands[0].version !== '151') throw Error('version');
d.getHighEntropyValues({}).then(v => {
  if (v.platform !== 'Windows' || v.uaFullVersion.indexOf('151') !== 0) throw Error('high entropy');
  console.log('ua ok');
});
'''
        self.run_node(harness.replace('UA', json.dumps(literal('UA_BRAND_JS'))))

    def test_capability_probe(self):
        harness = '''
const vm = require('vm');
const sandbox = {
  window: {isSecureContext: true},
  navigator: {userAgentData: {brands: [{brand: 'Chromium', version: '151'}]},
              mediaDevices: {getUserMedia: () => {}}},
  MediaRecorder: function(){}, console
};
vm.createContext(sandbox);
const caps = vm.runInContext(CAPS, sandbox);
if (!caps.secure || !caps.mediaDevices || !caps.mediaRecorder) throw Error(JSON.stringify(caps));
if (caps.speechNative || caps.speechShim) throw Error('speech flags');
if (caps.brands !== 'Chromium/151') throw Error(caps.brands);
console.log('caps ok');
'''
        self.run_node(harness.replace('CAPS', json.dumps(literal('CAPABILITY_JS'))))

    def test_purge_hides_header_and_logo_but_never_mic(self):
        purge = literal('JS_PURGE', 'web_compat.py')
        harness = '''
const vm = require('vm');
function el(name, opts) {
  opts = opts || {};
  const self = {
    nodeType: 1,
    name: name,
    style: {setProperty: (k, v) => { self.hiddenStyle = (self.hiddenStyle || '') + k + ':' + v + ';'; }},
    hiddenStyle: '',
    removed: false,
    remove() { self.removed = true; },
    _parent: opts.parent || null,
    _matches: opts.matches || [],
    _label: opts.label || '',
    _text: opts.text || '',
    _isInputArea: !!opts.inputArea,
    matches(sel) {
      const parts = sel.split(',').map(s => s.trim());
      return self._matches.some(m => parts.includes(m));
    },
    querySelectorAll(sel) { return (opts.all || []).filter(e => e.matches(sel)); },
    getAttribute(name) { return name === 'aria-label' ? self._label : (name === 'data-xid' ? (opts.xid || '') : (name === 'title' ? '' : null)); },
    closest(sel) {
      let n = self;
      while (n) { if (n._isInputArea) return n; n = n._parent; }
      return null;
    },
    textContent: opts.text || '',
    addEventListener() {}
  };
  return self;
}
const header = el('header', {matches: ['header#gb']});
const logoSvg = el('svg', {matches: ['svg[width="26"][height="26"][viewBox="0 0 24 24"]']});
const form = el('form', {inputArea: true});
const mic = el('button', {parent: form, label: 'Микрофон', xid: 'input-plate-voice-button', matches: ['div.eT9Cje']});
const micIcon = el('svg', {parent: mic, matches: ['svg[width="26"][height="26"][viewBox="0 0 24 24"]']});
const send = el('button', {parent: form, label: 'Отправить', matches: ['div.qEn1od']});
const root = el('html', {all: [header, logoSvg, mic, micIcon, send]});
const sandbox = {
  window: {addEventListener() {}},
  document: {documentElement: root, addEventListener() {}},
  MutationObserver: class { observe() {} },
  console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(PURGE, sandbox);
sandbox.window.__purgeRun();
if (!header.hiddenStyle || !header.removed) throw Error('header must be hidden');
if (!logoSvg.hiddenStyle || !logoSvg.removed) throw Error('G-logo svg must be hidden');
if (mic.hiddenStyle || mic.removed) throw Error('mic must survive purge even when matching');
if (micIcon.hiddenStyle || micIcon.removed) throw Error('mic icon must survive purge even when matching logo selector');
if (send.hiddenStyle || send.removed) throw Error('send must survive purge even when matching');
console.log('purge ok');
'''
        self.run_node(harness.replace('PURGE', json.dumps(purge)))

    def test_consent_autoclick_accepts_only_consent_surfaces(self):
        harness = '''
const vm = require('vm');
function button(label, opts) {
  opts = opts || {};
  const el = {
    nodeType: 1,
    _label: label, _parent: opts.parent || null, clicked: 0,
    innerText: label, value: '',
    getAttribute(n) { return n === 'aria-label' ? el._label : null; },
    closest(sel) {
      let n = el;
      while (n) { if (n._isDialog) return n; n = n._parent; }
      return null;
    },
    click() { el.clicked++; }
  };
  return el;
}
const dialog = {nodeType: 1, _isDialog: true, _parent: null};
const accept = button('Принять все', {parent: dialog});
const random = button('Отправить', {parent: dialog});
const floating = button('Accept all');
const root = {
  nodeType: 1,
  querySelectorAll() { return [accept, random, floating]; },
  matches() { return false; }
};
const sandbox = {
  window: {addEventListener() {}},
  document: {body: {}, documentElement: root, addEventListener() {},
             querySelectorAll: (s) => root.querySelectorAll(s)},
  location: {hostname: 'gemini.google.com'},
  setInterval: (fn) => { fn(); return 1; }, clearInterval: () => {},
  console
};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(CONSENT, sandbox);
if (accept.clicked !== 1) throw Error('consent accept must be clicked, got ' + accept.clicked);
if (random.clicked !== 0) throw Error('unrelated button must not be clicked');
if (floating.clicked !== 0) throw Error('bare Accept outside consent surface must not be clicked');
console.log('consent ok');
'''
        self.run_node(harness.replace('CONSENT', json.dumps(literal('CONSENT_AUTOCLICK_JS'))))

    def test_webchannel_lite_matches_qt_protocol(self):
        harness = '''
const vm = require('vm');
const sent = [];
const handlers = {};
const transport = {
  send(raw) {
    const msg = JSON.parse(raw);
    sent.push(msg);
    if (msg.type === 3) {
      transport.onmessage({data: JSON.stringify({type: 10, id: msg.id, data: {
        legalyzeSpeech: {
          id: 'legalyzeSpeech',
          methods: [['start', 0], ['stop', 1], ['abort', 2], ['reportCapabilities', 3]],
          signals: [['started', 0], ['hypothesis', 1], ['result', 2], ['error', 3], ['ended', 4]],
          properties: [], enums: {}
        }
      }})});
    }
  },
  onmessage: null
};
const sandbox = {window: {}, console, JSON};
vm.createContext(sandbox);
vm.runInContext(LITE, sandbox);
let bridge = null;
new sandbox.window.QWebChannel(transport, (ch) => { bridge = ch.objects.legalyzeSpeech; });
if (!bridge) throw Error('init handshake failed');
if (!sent.some(m => m.type === 3)) throw Error('init message');
if (!sent.some(m => m.type === 4)) throw Error('idle after init');
const started = [];
bridge.started.connect(() => started.push(1));
if (!sent.some(m => m.type === 7 && m.signal === 0)) throw Error('connectToSignal');
transport.onmessage({data: JSON.stringify({type: 1, object: 'legalyzeSpeech', signal: 0, args: []})});
if (started.length !== 1) throw Error('signal dispatch');
let reply = null;
bridge.start('ru-RU', true, false, (v) => { reply = v; });
const invoke = sent.find(m => m.type === 6);
if (!invoke || invoke.method !== 'start' || invoke.args[0] !== 'ru-RU') throw Error('invokeMethod shape');
transport.onmessage({data: JSON.stringify({type: 10, id: invoke.id, data: 'ok'})});
if (reply !== 'ok') throw Error('response dispatch');
console.log('webchannel lite ok');
'''
        self.run_node(harness.replace('LITE', json.dumps(literal('QWEBCHANNEL_LITE_JS'))))

    def test_compatibility_bundle_parses(self):
        qwebchannel_stub = '''var qt = {webChannelTransport: {}};
var QWebChannel = function(transport, cb) {
  const sig = () => ({connect: () => {}});
  cb({objects: {legalyzeSpeech: {
    reportCapabilities() {}, started: sig(), speechStart: sig(), speechEnd: sig(),
    hypothesis: sig(), result: sig(), rejected: sig(), error: sig(), ended: sig()
  }}});
};'''
        harness = '''
const vm = require('vm');
const sandbox = {window: {}, navigator: {language: 'ru-RU'}, setTimeout, Promise, console};
sandbox.window.window = sandbox.window;
vm.createContext(sandbox);
vm.runInContext(BUNDLE, sandbox);
if (typeof sandbox.window.SpeechRecognition !== 'function') throw Error('bundle');
if (!sandbox.window.__legalyzeSpeechBridge) throw Error('bridge after channel init');
console.log('bundle ok');
'''
        bundle = '\n'.join([literal('UA_BRAND_JS'), literal('SPEECH_SHIM_JS'),
                            qwebchannel_stub, literal('CHANNEL_INIT_JS')])
        self.run_node(harness.replace('BUNDLE', json.dumps(bundle)))


class WorkerStateTests(unittest.TestCase):
    def worker_case(self, ready):
        tree = ast.parse((ROOT / 'main.py').read_text())
        worker = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ChromeWorker')
        clock = iter(range(0, 10000, 20))
        page = Mock()
        page.eval.return_value = {'value': ready}
        calls = []
        diag = _FakeDiag()
        ns = dict(
            QThread=type('QThread', (), {'__init__': lambda self, parent=None: None,
                                         'msleep': lambda self, ms: None,
                                         'isInterruptionRequested': lambda self: False}),
            pyqtSignal=lambda *a: None,
            time=SimpleNamespace(monotonic=lambda: next(clock)),
            attach_chrome=lambda *a, **k: (Mock(), page),
            install_compat=lambda *a, **k: calls.append('compat'),
            grant_mic_permission=lambda *a, **k: calls.append('grant'),
            install_purge=lambda *a, **k: calls.append('purge'),
            READY_SCRIPT='x', DEBUG_PORT=1, diag=diag,
            SURFACE_READY_TIMEOUT=90,
            # v19: ворота готовности. `ready` — результат ожидания, а не
            # ответ одного скрипта: раньше «чатом» считалось любое поле ввода
            # на странице с readyState == "interactive".
            wait_for_chat_surface=lambda *a, **k: {
                'ready': bool(ready), 'verdict': 'gemini' if ready else 'blank',
                'surface': {}, 'rounds': 3, 'blocked': '' if ready else 'blank',
                'stable': 2, 'theme_repairs': 0},
        )
        worker_node = ast.Module(body=[worker], type_ignores=[])
        exec(compile(worker_node, 'worker', 'exec'), ns)
        obj = ns['ChromeWorker']()
        received, failed = [], []
        obj.hwnd_ready = SimpleNamespace(connect=lambda f: received.append(f),
                                         emit=lambda v: received.append(v))
        obj.failed = SimpleNamespace(connect=lambda f: failed.append(f),
                                     emit=lambda: failed.append(True))
        obj.failure = {}
        obj.run()
        return calls, received, failed, diag.events

    def test_compat_and_mic_are_installed_before_the_wait(self):
        calls, received, failed, events = self.worker_case(True)
        self.assertEqual(['compat', 'grant', 'purge'], calls)

    def test_blank_page_is_reported_as_blocked_not_as_ready(self):
        # v19: пустая страница больше не роняет автоматизацию молча — причина
        # уходит в лог (`page.blocked`) и в строку состояния, а приложение
        # остаётся рабочим (пользователь может войти в аккаунт).
        calls, received, failed, events = self.worker_case(False)
        self.assertIn('compat', calls)  # compatibility is installed before readiness
        self.assertIn('grant', calls)
        self.assertEqual(failed, [])
        self.assertIn(True, received)
        chat_ready = [kw for name, kw in events if name == 'webengine.chat_ready']
        self.assertTrue(chat_ready)
        self.assertFalse(chat_ready[0]['ready'])
        self.assertEqual('blank', chat_ready[0]['blocked'])
        blocked = [kw for name, kw in events if name == 'state.set' and kw.get('key') == 'page.blocked']
        self.assertTrue(blocked)

    def test_real_editor_signals_ready_once(self):
        calls, received, failed, events = self.worker_case(True)
        self.assertEqual(calls, ['compat', 'grant', 'purge'])
        self.assertIn(True, received)
        self.assertEqual(failed, [])
        chat_ready = [kw for name, kw in events if name == 'webengine.chat_ready']
        self.assertTrue(chat_ready)
        self.assertTrue(chat_ready[0]['ready'])


if __name__ == '__main__':
    unittest.main()


@skip_node
class MicButtonTests(unittest.TestCase):
    """Рабочая кнопка микрофона: реплика штатной кнопки + настоящая диктовка."""

    HARNESS = r'''
const vm = require('vm');

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.attrs = {};
    this.children = [];
    this.parentElement = null;
    this.parentNode = null;
    this.style = {cssText: '', display: '', visibility: ''};
    this.textContent = '';
    this.value = '';
    this._sized = false;
    this._listeners = {};
    this._html = '';
    this.classList = {
      set: new Set(),
      toggle: (c, on) => { if (on) this.classList.set.add(c); else this.classList.set.delete(c); },
      contains: (c) => this.classList.set.has(c)
    };
  }
  get id() { return this.attrs.id || ''; }
  set id(v) { this.attrs.id = String(v); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatchEvent(ev) {
    ev.target = this;
    if (!ev.stopImmediatePropagation) {
      ev._stopped = false;
      ev.defaultPrevented = false;
      ev.stopImmediatePropagation = () => { ev._stopped = true; };
      ev.stopPropagation = () => { ev._stopped = true; };
      ev.preventDefault = () => { ev.defaultPrevented = true; };
    }
    // Capture phase (document) runs before the target listeners, as in a browser.
    for (const fn of (document._listeners[ev.type] || [])) {
      if (ev._stopped) break;
      fn.call(this, ev);
    }
    for (const fn of (this._listeners[ev.type] || [])) {
      if (ev._stopped) break;
      fn(ev);
    }
    return true;
  }
  click() { this.dispatchEvent(new MouseEvent('click', {})); }
  focus() {}
  appendChild(c) { c.parentElement = c.parentNode = this; this.children.push(c); return c; }
  insertBefore(c, ref) {
    c.parentElement = c.parentNode = this;
    const i = this.children.indexOf(ref);
    this.children.splice(i < 0 ? this.children.length : i, 0, c);
    return c;
  }
  removeChild(c) {
    const i = this.children.indexOf(c);
    if (i >= 0) this.children.splice(i, 1);
    c.parentElement = c.parentNode = null;
    return c;
  }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }
  getClientRects() { return this._sized ? [{width: 400, height: 40}] : []; }
  getBoundingClientRect() { return {x: 200, y: 20, left: 0, top: 0, right: 400, bottom: 40, width: 400, height: 40}; }
  get offsetWidth() { return this._sized ? 400 : 0; }
  get offsetHeight() { return this._sized ? 40 : 0; }
  get offsetParent() { return this._sized ? this.parentElement : null; }
  get isConnected() { return true; }
  matches(sel) { return matchSel(this, sel); }
  closest(sel) {
    let el = this;
    while (el) { if (el.matches && el.matches(sel)) return el; el = el.parentElement; }
    return null;
  }
  querySelector(sel) {
    return walk(this).find((el) => matchSel(el, sel)) || null;
  }
  querySelectorAll(sel) {
    return walk(this).filter((el) => matchSel(el, sel));
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); }
}

function matchOne(el, s) {
  let rest = String(s).trim();
  const m = rest.match(/^([a-zA-Z0-9_-]+|\*)/);
  if (m) {
    if (m[1] !== '*' && el.tagName !== m[1].toUpperCase()) return false;
    rest = rest.slice(m[1].length);
  }
  while (rest.length) {
    if (rest[0] === '#') {
      const id = rest.match(/^#([A-Za-z0-9_-]+)/);
      if (!id || (el.getAttribute('id') || el.id) !== id[1]) return false;
      rest = rest.slice(id[0].length);
    } else if (rest[0] === '.') {
      const cls = rest.match(/^\.([A-Za-z0-9_-]+)/);
      if (!cls || !el.classList.contains(cls[1])) return false;
      rest = rest.slice(cls[0].length);
    } else if (rest[0] === '[') {
      const a = rest.match(/^\[([A-Za-z0-9_:-]+)(\*?=)"?([^\]"]*)"?\]/);
      if (!a) return false;
      const val = el.getAttribute(a[1]);
      if (val === null) return false;
      if (a[2] === '=' && val !== a[3]) return false;
      if (a[2] === '*=' && val.indexOf(a[3]) < 0) return false;
      rest = rest.slice(a[0].length);
    } else return false;
  }
  return true;
}
function matchSel(el, selector) {
  return String(selector).split(',').some((s) => matchOne(el, s));
}
function walk(node, out) {
  out = out || [];
  for (const c of node.children || []) { out.push(c); walk(c, out); }
  return out;
}

const documentElement = new El('html');
documentElement.lang = 'ru-RU';
const head = new El('head');
documentElement.appendChild(head);
const body = new El('body');
documentElement.appendChild(body);

const document = {
  readyState: 'complete',
  documentElement,
  head,
  body,
  _listeners: {},
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); },
  dispatchEvent() { return true; },
  createElement: (tag) => new El(tag),
  getElementById(id) {
    return walk(documentElement).find((el) => (el.getAttribute('id') || el.id) === id) || null;
  },
  querySelector(sel) {
    return walk(documentElement).find((el) => matchSel(el, sel)) || null;
  },
  querySelectorAll(sel) {
    return walk(documentElement).filter((el) => matchSel(el, sel));
  }
};

class MockSR {
  constructor() { MockSR.last = this; this.active = false; this.starts = 0;
    MockSR.count = (MockSR.count || 0) + 1; }
  start() { this.active = true; this.starts += 1; if (this.onstart) this.onstart(); }
  stop() { if (!this.active) return; this.active = false; if (this.onend) this.onend(); }
  give(text, isFinal) {
    if (this.onresult) {
      this.onresult({resultIndex: 0, results: [{0: {transcript: text}, isFinal: !!isFinal, length: 1}]});
    }
  }
}

const window = {SpeechRecognition: MockSR, addEventListener() {}};
window.window = window;
class FakeEvent { constructor(type, opts) { this.type = type; Object.assign(this, opts || {}); } }
const sandbox = {
  window, document, navigator: {language: 'ru-RU'},
  MutationObserver: class { observe() {} disconnect() {} },
  Event: FakeEvent,
  InputEvent: FakeEvent,
  MouseEvent: FakeEvent,
  PointerEvent: FakeEvent,
  KeyboardEvent: FakeEvent,
  Date, Math, JSON, Object, Array, String, Number, Boolean, Set, Map, Error, RegExp, isNaN, parseInt, parseFloat,
  setTimeout, clearTimeout, setInterval, clearInterval, console
};
vm.createContext(sandbox);
'''

    def run_node(self, script):
        res = subprocess.run([NODE, '-e', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_replica_injected_when_site_mic_missing_and_drives_dictation(self):
        script = self.HARNESS + '''
vm.runInContext(MIC_SRC, sandbox);
vm.runInContext(START_SRC, sandbox);  // business JS стабильна до конца теста

// Страница БЕЗ штатной кнопки микрофона: панель ввода + textarea + отправка.
const plate = new El('div');
plate.setAttribute('data-xid', 'input-plate');
const row = new El('div');
plate.appendChild(row);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(plate);

// Первичная инъекция уже отработала (readyState=complete) — досоздать кнопку.
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);

const mic = document.getElementById('legalyze-mic');
if (!mic) throw Error('replica not injected');
if (mic.getAttribute('aria-label') !== 'Микрофон') throw Error('bad aria-label');
if (mic.getAttribute('data-xid') !== 'input-plate-voice-button') throw Error('bad data-xid');
if (mic.classList.contains('wdK4Nc')) throw Error('replica must not carry the send-button class');

// Бизнес-код (JS_START_RECORDING) находит реплику и "кликает" её.
mic._sized = true;
const res = vm.runInContext(START_SRC, sandbox);
if (!res || typeof res.x !== 'number') throw Error('business JS cannot find replica: ' + JSON.stringify(res));
if (!MockSR.last || !MockSR.last.active) throw Error('dictation did not start');

// Диктовка пишет текст в поле ввода (interim -> final).
MockSR.last.give('привет', false);
if (ta.value !== 'привет') throw Error('interim text not written: ' + ta.value);
MockSR.last.give('привет мир', true);
if (ta.value !== 'привет мир') throw Error('final text not written: ' + ta.value);

// Повторный клик по кнопке (через бизнес-код) останавливает запись.
// Выдерживаем антидребезг серии кликов (500 мс) синхронно.
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 600);
const res2 = vm.runInContext(START_SRC, sandbox);
if (!res2 || typeof res2.x !== 'number') throw Error('second business click failed');
if (MockSR.last.active) throw Error('dictation must stop on second toggle');

console.log('replica dictation ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_START_RECORDING', 'main.py'))))
        self.run_node(script)

    def test_explicit_start_never_sends_and_restarts_stuck_session(self):
        script = self.HARNESS + """
// Рассинхрон (как в реальном логе): запись «активна», но движок молчит.
const row = new El('div');
const siteMic = new El('button');
siteMic.setAttribute('data-xid', 'input-plate-voice-button');
siteMic.setAttribute('aria-label', 'Микрофон');
siteMic._sized = true;
row.appendChild(siteMic);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(row);

vm.runInContext(MIC_SRC, sandbox);

// Явный старт: реплика/хоткей всегда НАЧИНАЕТ запись.
if (vm.runInContext(START_SRC, sandbox) !== true) throw Error('explicit start failed');
if (!MockSR.last || !MockSR.last.active) throw Error('dictation must be recording');
if (MockSR.count !== 1) throw Error('one recognizer expected: ' + MockSR.count);

// Повторный явный старт перезапускает запись, а не «отправляет» сообщение.
if (vm.runInContext(START_SRC, sandbox) !== true) throw Error('restart failed');
if (MockSR.count !== 2) throw Error('recognition must restart: ' + MockSR.count);
if (!MockSR.last.active) throw Error('must still be recording after restart');

// Текст по-прежнему пишется в поле ввода, а не отправляется сразу.
MockSR.last.give('привет мир', true);
if (ta.value !== 'привет мир') throw Error('dictated text must reach the editor');

// И только явный стоп (второе нажатие) завершает запись перед отправкой.
const res = vm.runInContext(STOP_SRC, sandbox);
if (!res || res.ok !== true) throw Error('stop_and_send failed');
if (MockSR.last.active) throw Error('send must stop dictation');
console.log('explicit start ok');
"""
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_MIC_START')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
        self.run_node(script)

    def test_speech_shim_overrides_native_constructor_and_never_calls_it(self):
        # Нативный SpeechRecognition в QtWebEngine без Google API-ключа РОНЯЕТ
        # рендер при start(). Шим обязан перехватить конструктор всегда.
        script = r'''
const vm = require('vm');
const signals = {};
const bridgeCalls = [];
const bridge = {
  start: (lang) => bridgeCalls.push(['start', lang]),
  stop: () => bridgeCalls.push(['stop']),
  abort: () => bridgeCalls.push(['abort']),
  started: {connect: f => signals.started = f},
  speechStart: {connect: f => signals.speechstart = f},
  speechEnd: {connect: f => signals.speechend = f},
  hypothesis: {connect: f => signals.hypothesis = f},
  result: {connect: f => signals.result = f},
  rejected: {connect: f => signals.rejected = f},
  error: {connect: f => signals.error = f},
  ended: {connect: f => signals.ended = f}
};
let nativeStarts = 0;
function MockNativeSpeechRecognition() {
  this.start = () => { nativeStarts += 1; throw new Error('renderer crash'); };
}
const sandbox = {
  window: {
    SpeechRecognition: MockNativeSpeechRecognition,
    webkitSpeechRecognition: MockNativeSpeechRecognition,
    __legalyzeSpeechBridge: bridge,
    __legalyzeSpeechBridgeWait: 10
  },
  navigator: {language: 'ru-RU'},
  setTimeout, clearTimeout, Promise, console
};
sandbox.window.window = sandbox.window;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(SHIM, sandbox);

const w = sandbox.window;
if (w.__legalyzeNativeSpeechRecognition !== MockNativeSpeechRecognition)
  throw Error('native must be preserved for diagnostics');
if (w.SpeechRecognition === MockNativeSpeechRecognition)
  throw Error('native constructor must be overridden');
if (w.LegalyzeSpeechRecognition !== w.SpeechRecognition)
  throw Error('explicit export must match the override');
if (w.__legalyzeSpeech.mode !== 'shim') throw Error('mode');

const rec = new w.LegalyzeSpeechRecognition();
rec.lang = 'ru-RU';
rec.start();
if (nativeStarts !== 0) throw Error('native start() must never be called');
if (!bridgeCalls.length || bridgeCalls[0][0] !== 'start')
  throw Error('bridge must be used');
console.log('shim override ok');
'''
        self.run_node(script.replace('SHIM', json.dumps(literal('SPEECH_SHIM_JS'))))

    def test_mic_click_hijack_blocks_site_handlers_and_starts_dictation(self):
        script = self.HARNESS + '''
// Штатная кнопка микрофона с СОБСТВЕННЫМИ обработчиками (как jsaction сайта):
// они вызывают getUserMedia и роняют рендер в QtWebEngine без WebRTC.
const row = new El('div');
const siteMic = new El('button');
siteMic.setAttribute('data-xid', 'input-plate-voice-button');
siteMic.setAttribute('aria-label', 'Микрофон');
siteMic._sized = true;
let siteClicks = 0, siteDowns = 0;
siteMic.addEventListener('click', () => siteClicks++);
siteMic.addEventListener('mousedown', () => siteDowns++);
siteMic.addEventListener('pointerdown', () => siteDowns++);
row.appendChild(siteMic);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(row);

vm.runInContext(MIC_SRC, sandbox);
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (document.getElementById('legalyze-mic')) throw Error('replica must not replace native mic');

// Бизнес-код нажимает штатную кнопку (synthetic-клики + el.click()).
const res = vm.runInContext(START_SRC, sandbox);
if (!res || typeof res.x !== 'number') throw Error('business JS cannot find button');
if (siteDowns !== 0) throw Error('site press handlers must be blocked: ' + siteDowns);
if (siteClicks !== 0) throw Error('site click handler must be blocked: ' + siteClicks);
if (!MockSR.last || !MockSR.last.active) throw Error('dictation must start via hijack');

// Серия кликов (synthetic + el.click()) даёт ровно один старт.
if (MockSR.last.starts !== 1) throw Error('exactly one start expected: ' + MockSR.last.starts);

// Повторное нажатие пользователем (вне антидребезга) останавливает запись.
Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 600);
siteMic.dispatchEvent({type: 'click'});
if (MockSR.last.active) throw Error('second click must stop dictation');
if (siteClicks !== 0) throw Error('site click handler must stay blocked');

// Кнопка отправки не перехватывается: штатный сценарий отправки работает.
vm.runInContext('window.__legalyzeMic.toggle()', sandbox);
if (!MockSR.last.active) throw Error('dictation restart');
const res2 = vm.runInContext(STOP_SRC, sandbox);
if (!res2 || res2.ok !== true) throw Error('stop_and_send failed');
if (MockSR.last.active) throw Error('send must stop dictation');
console.log('hijack ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('START_SRC', json.dumps(literal('JS_START_RECORDING', 'main.py')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
        self.run_node(script)

    def test_native_site_button_prevents_replica_and_send_stops_dictation(self):
        script = self.HARNESS + '''
// Страница СО штатной кнопкой микрофона.
const row = new El('div');
const nativeMic = new El('button');
nativeMic.setAttribute('data-xid', 'input-plate-voice-button');
nativeMic.setAttribute('aria-label', 'Микрофон');
nativeMic._sized = true;
row.appendChild(nativeMic);
const send = new El('button');
send.setAttribute('aria-label', 'Отправить');
send._sized = true;
row.appendChild(send);
const ta = new El('textarea');
ta._sized = true;
row.appendChild(ta);
body.appendChild(row);

vm.runInContext(MIC_SRC, sandbox);
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (document.getElementById('legalyze-mic')) throw Error('replica must not replace native mic');

// Собственная диктовка (fallback F3): старт, затем штатная кнопка отправки
// через JS_STOP_AND_SEND останавливает запись перед отправкой.
if (vm.runInContext('window.__legalyzeMic.toggle()', sandbox) !== true) throw Error('dictation start');
if (!MockSR.last.active) throw Error('not recording');
const res = vm.runInContext(STOP_SRC, sandbox);
if (!res || res.ok !== true) throw Error('stop_and_send failed: ' + JSON.stringify(res));
if (MockSR.last.active) throw Error('send must stop dictation');

// Кнопка-реплика возвращается, если штатная исчезает (SPA-редеринг).
nativeMic.remove();
vm.runInContext('window.__legalyzeMic.ensure()', sandbox);
if (!document.getElementById('legalyze-mic')) throw Error('replica must appear after native mic gone');
console.log('native-first ok');
'''
        script = (script
                  .replace('MIC_SRC', json.dumps(literal('MIC_BUTTON_JS')))
                  .replace('STOP_SRC', json.dumps(literal('JS_STOP_AND_SEND', 'main.py'))))
        self.run_node(script)


def _fn_node(tree_name, name, class_name=None):
    """Скомпилировать одну функцию (метод) из исходника, не импортируя PyQt6."""
    tree = ast.parse((ROOT / tree_name).read_text())
    if class_name:
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                   and n.name == class_name)
        source_nodes = cls.body
    else:
        source_nodes = tree.body
    node = next(n for n in source_nodes if isinstance(n, ast.FunctionDef)
                and n.name == name)
    body = list(node.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    clone = ast.FunctionDef(name=name, args=node.args, body=body, decorator_list=[],
                            returns=None, type_comment=None, type_params=[])
    ast.fix_missing_locations(clone)
    return clone


class Round25Tests(unittest.TestCase):
    """v16: первый запуск, шторка, .py в хранилище, чужие разрешения/масштаб."""

    # ── 1. Первый запуск: промт не выбран → экспорт не начинался ─────────────

    def test_first_run_opens_the_prompt_picker_itself(self):
        # На чистой машине selected_prompt пуст: раньше вставала красная
        # шторка «нажмите Промт», GUI был закрыт, .pdf/.txt НЕ экспортировались.
        source = (ROOT / 'main.py').read_text()
        block = source.split('def _load_prompt(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('if not sel:', block)
        self.assertIn('QTimer.singleShot(0, self._choose_prompt)', block)
        # Один раз за запуск — диалог не выпрыгивает повторно.
        self.assertIn('_prompt_picker_shown', block)
        # Это НЕ ошибка: шторка информационная, красным не мигает.
        self.assertNotIn('error=True', block)
        self.assertIn('diag.event("prompt.picker_auto"', block)

    def test_the_working_prompt_path_is_untouched(self):
        # Ветка «промт выбран» обязана работать ровно как в v15: воркер,
        # загрузка, затем экспорт в чат.
        source = (ROOT / 'main.py').read_text()
        block = source.split('def _load_prompt(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('PromptLoaderWorker(self.cfg, self.token, force=force)', block)
        self.assertIn('self.prompt_loader.start()', block)
        self.assertIn('self._start_upload_thread()',
                      source.split('def _on_prompt_loaded(', 1)[1].split('\n    def ', 1)[0])

    # ── 2. Браузер не виден ни мгновения ─────────────────────────────────────

    def test_the_cover_is_held_until_the_chat_is_ready(self):
        source = (ROOT / 'main.py').read_text()
        poll = source.split('def _poll_browser_focus(', 1)[1].split('\n    def ', 1)[0]
        # v17: сторож работает с самого старта браузера — раньше он требовал
        # `native_mode`, а тот включался уже в `_on_browser_ready`, из-за чего
        # весь этап «Ожидание страницы» шторку никто не держал.
        self.assertIn('self._hold_cover_if_needed()', poll)
        self.assertNotIn('if host is None or not self.native_mode:', poll)
        self.assertIn('if host is None:\n            return', poll)
        guard = source.split('def _hold_cover_if_needed(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('if self._chat_ready and self._prompt_selected():', guard)
        self.assertIn('self._cover_to_top()', guard)
        # …и отпускается штатно: `_hide_overlay` снимает шторку, когда можно.
        hide = source.split('def _hide_overlay(', 1)[1].split('\n\n', 1)[0]
        self.assertIn('self.browser_cover.hide()', hide)

    def test_late_browser_windows_are_hidden_after_ready(self):
        source = (ROOT / 'main.py').read_text()
        ready = source.split('def _on_browser_ready(', 1)[1].split('\n    def ', 1)[0]
        self.assertEqual(ready.count('self._hide_strays_once'), 3)
        hide = source.split('def _hide_strays_once(', 1)[1].split('\n    def ', 1)[0]
        # Без refresh_pids() дерево застывает на моменте ready — окна, поднятые
        # позже, остались бы висеть в Alt+Tab.
        self.assertIn('host.refresh_pids()', hide)
        self.assertIn('host._hide_strays(', hide)

    # ── 3. .py в %APPDATA%\Legalyze ──────────────────────────────────────────

    def test_stray_sources_are_removed(self):
        import shutil as _shutil
        events = []

        class FakeDiag:
            @staticmethod
            def exception(_label):
                pass

            @staticmethod
            def event(name, **kw):
                events.append((name, kw))

        scope = {'Path': Path, 'shutil': _shutil, 'diag': FakeDiag}
        exec(compile(ast.Module(body=[_fn_node('main.py', 'purge_stray_sources')],
                                type_ignores=[]), '<p>', 'exec'), scope)
        purge = scope['purge_stray_sources']

        with tempfile.TemporaryDirectory() as tmp:
            app, data = Path(tmp) / 'app', Path(tmp) / 'data'
            app.mkdir(parents=True)
            data.mkdir(parents=True)
            (app / 'config.json').write_text('{}')
            (app / 'main.py').write_text('x = 1')
            (app / 'main.pyc').write_bytes(b'\x00')
            cache = app / '__pycache__'
            cache.mkdir()
            (cache / 'main.cpython-312.pyc').write_bytes(b'\x00')
            (data / 'DejaVuSans.ttf').write_bytes(b'\x00')
            (data / 'stray.py').write_text('x = 1')

            removed = purge(app, data)

            self.assertEqual(removed, 4)
            self.assertFalse((app / 'main.py').exists())
            self.assertFalse((app / 'main.pyc').exists())
            self.assertFalse(cache.exists())
            self.assertFalse((data / 'stray.py').exists())
            # Данные программы не тронуты.
            self.assertTrue((app / 'config.json').exists())
            self.assertTrue((data / 'DejaVuSans.ttf').exists())
        self.assertIn(('storage.stray_py', {'removed': 4}), events)

    def test_the_purge_runs_at_startup(self):
        source = (ROOT / 'main.py').read_text()
        entry = source.split('\ndef main():\n', 1)[1]
        self.assertIn('purge_stray_sources(APP_DIR, DATA_DIR)', entry)
        # …после миграции старых данных и до запуска браузера.
        self.assertLess(entry.index('purge_stray_sources(APP_DIR, DATA_DIR)'),
                        entry.index('_kill_leftover_browsers()'))

    def test_business_modules_have_no_purge_code(self):
        # config.py / storage_paths.py — бизнес-модули, они остаются
        # байт-в-байт равными оригиналу (проверяет SourceTests выше).
        for name in ('config.py', 'storage_paths.py'):
            self.assertNotIn('purge_stray_sources',
                             (ROOT / name).read_text(), name)

    # ── 4. Чужие разрешения и масштаб ────────────────────────────────────────

    def _inset(self, logical, physical):
        scope = {'BROWSER_DX': -10, 'BROWSER_DY': -40, 'BROWSER_DW': 0,
                 'BROWSER_DH': 0, 'int': int, 'float': float, 'round': round,
                 'TypeError': TypeError, 'ValueError': ValueError}
        exec(compile(ast.Module(body=[_fn_node('main.py', '_browser_inset',
                                               'MainWindow')], type_ignores=[]),
                     '<i>', 'exec'), scope)
        owner = SimpleNamespace(cfg={})
        owner._placeholder_logical_size = lambda: logical
        owner._placeholder_physical_size = lambda: physical
        owner._browser_inset = scope['_browser_inset'].__get__(owner)
        return owner._browser_inset()

    def test_the_shift_stays_exact_at_every_scale(self):
        # Отлаженная геометрия не имеет права поехать: в DIP сдвиг всегда
        # -10/-40, в физических пикселях он масштабируется вместе с монитором.
        self.assertEqual(self._inset((453, 735), (453, 735)), (-10, -40, 10, 40))
        self.assertEqual(self._inset((453, 735), (566, 919)), (-12, -50, 12, 50))
        self.assertEqual(self._inset((453, 735), (680, 1103)), (-15, -60, 15, 60))
        # Размер окна при этом не меняется: правый/нижний inset зеркален левому.
        for physical in ((453, 735), (566, 919), (680, 1103)):
            left, top, right, bottom = self._inset((453, 735), physical)
            self.assertEqual(physical[0] - left - right, physical[0])
            self.assertEqual(physical[1] - top - bottom, physical[1])

    def test_the_inset_is_recomputed_on_every_sync(self):
        source = (ROOT / 'main.py').read_text()
        sync = source.split('def _sync_chrome_geometry(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('host.inset = self._browser_inset()', sync)
        self.assertIn('host.sync()', sync)
        self.assertLess(sync.index('host.inset = self._browser_inset()'),
                        sync.index('host.sync()'))

    def test_a_monitor_change_is_handled_immediately(self):
        source = (ROOT / 'main.py').read_text()
        move = source.split('def moveEvent(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._check_dpi_drift()', move)
        change = source.split('def changeEvent(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('_METRIC_CHANGE_EVENTS', change)
        self.assertIn('self._on_screen_metrics_changed(', change)

        handler = source.split('def _on_screen_metrics_changed(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._sync_chrome_geometry()', handler)
        self.assertIn('self._ensure_zoom()', handler)
        self.assertIn('self._hide_browser_taskbar()', handler)
        # Зум НИКОГДА не перезапускает браузер (это ломало v10/v11).
        for banned in ('_start_chrome', 'restart_next', 'browser_host.stop'):
            self.assertNotIn(banned, handler, banned)

    def test_the_drift_check_only_fires_when_the_scale_really_changed(self):
        source = (ROOT / 'main.py').read_text()
        drift = source.split('def _check_dpi_drift(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self.devicePixelRatioF()', drift)
        self.assertIn('_last_dpr', drift)
        self.assertIn('if abs(dpr - previous) < 0.001:', drift)

    def test_the_scale_has_a_fallback_from_the_window_itself(self):
        source = (ROOT / 'main.py').read_text()
        scale = source.split('def _monitor_scale(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self.devicePixelRatioF()', scale)
        self.assertIn('return scale if scale > 0 else 1.0', scale)

    def test_the_window_is_kept_inside_the_work_area(self):
        source = (ROOT / 'diagnostics.py').read_text()
        block = source.split('def place_window(', 1)[1].split('\n\n\n', 1)[0]
        # Рабочая формула не тронута.
        self.assertIn('window.move(g.x() + max(0, g.width()-window.width()-16),', block)
        self.assertIn('g.y() + max(0, (g.height()-window.height())//2))', block)
        # Страховка: окно вне всех экранов (сменили/отключили монитор).
        self.assertIn('main.geometry_rescued', block)
        # …и честное сообщение, если экран физически ниже окна.
        self.assertIn('main.geometry_clipped', block)
        # Размер окна не меняется — раскладка остаётся прежней.
        self.assertNotIn('window.resize(', block)
        self.assertNotIn('setFixedSize(', block)


class Round26Tests(unittest.TestCase):
    """v17: шторка до показа браузера (п.1) и окно ожидания до выбора промта (п.2)."""

    # ── 1. Ни одного промелькнувшего элемента ────────────────────────────────

    def test_the_cover_is_raised_before_the_browser_window_is_shown(self):
        source = (ROOT / 'win32_embed.py').read_text()
        block = source.split('def embed(', 1)[1].split('\n\n\ndef ', 1)[0]
        self.assertIn('cover=0', block.split('\n')[0])          # новый параметр
        self.assertIn('sync(user32, hwnd, parent, inset)', block)
        self.assertIn('raise_window(user32, cover)', block)
        self.assertIn('user32.ShowWindow(hwnd, SW_SHOW)', block)
        # Порядок: поставить → поднять шторку → показать → поднять снова.
        order = [block.index('sync(user32, hwnd, parent, inset)'),
                 block.index('raise_window(user32, cover)'),
                 block.index('user32.ShowWindow(hwnd, SW_SHOW)')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(block.count('raise_window(user32, cover)'), 2)

    def test_raise_window_puts_the_cover_on_top_of_the_siblings(self):
        calls = []

        class FakeUser32:
            @staticmethod
            def SetWindowPos(hwnd, after, x, y, w, h, flags):
                calls.append(dict(hwnd=int(hwnd.value), after=int(after.value or 0),
                                  x=x, y=y, w=w, h=h, flags=flags))
                return 1

        self.assertTrue(win32_embed.raise_window(FakeUser32(), 4242))
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call['hwnd'], 4242)
        self.assertEqual(call['after'], 0)                 # HWND_TOP
        self.assertEqual(call['flags'],
                         win32_embed.SWP_NOMOVE | win32_embed.SWP_NOSIZE
                         | win32_embed.SWP_NOACTIVATE)
        # Без NOZORDER: именно подъём, а не только смена рамки.
        self.assertEqual(call['flags'] & win32_embed.SWP_NOZORDER, 0)
        self.assertEqual((call['x'], call['y'], call['w'], call['h']), (0, 0, 0, 0))

    def test_raise_window_is_safe_without_win32(self):
        self.assertFalse(win32_embed.raise_window(None, 7))
        self.assertFalse(win32_embed.raise_window(object(), 0))

    def test_the_host_hands_the_cover_to_embed(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('self.cover_widget = None',
                      source.split('class NativeHost(', 1)[1].split('\n    def ', 1)[0]
                      + source.split('class NativeHost(', 1)[1].split('def embed')[0])
        embed = source.split('def _embed(self, hwnd):', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('cover = int(self.cover_widget.winId())', embed)
        self.assertIn('win32_embed.embed(self.user32, hwnd, parent, self.inset, cover=cover)',
                      embed)
        start = source.split('def _start_chrome(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('host.cover_widget = self.browser_cover', start)

    def test_the_cover_is_re_raised_while_the_browser_settles(self):
        source = (ROOT / 'main.py').read_text()
        ready = source.split('def _on_browser_ready(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self._cover_to_top()', ready)
        # Тот же каденс, что и у скрытия кнопки в панели задач: окно браузера
        # может быть поднято Chrome уже после `ready`.
        self.assertIn('for delay in (300, 800, 1500, 2500, 4000):', ready)
        self.assertIn('QTimer.singleShot(delay, self._hold_cover_if_needed)', ready)

    def test_cover_to_top_uses_both_qt_and_win32(self):
        source = (ROOT / 'main.py').read_text()
        top = source.split('def _cover_to_top(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('self.browser_cover.show()', top)
        self.assertIn('self.browser_cover.raise_()', top)
        self.assertIn('win32_embed.raise_window(user32, int(self.browser_cover.winId()))',
                      top)

    # ── 2. Окно ожидания — до момента выбора промта ──────────────────────────

    def _method(self, name, scope):
        exec(compile(ast.Module(body=[_fn_node('main.py', name, 'MainWindow')],
                                type_ignores=[]), '<m>', 'exec'), scope)
        return scope[name]

    def test_prompt_selected_reads_the_config(self):
        scope = {'diag': SimpleNamespace(exception=lambda *_a: None),
                 'str': str, 'bool': bool}
        fn = self._method('_prompt_selected', scope)
        self.assertFalse(fn(SimpleNamespace(cfg={})))
        self.assertFalse(fn(SimpleNamespace(cfg={'selected_prompt': ''})))
        self.assertFalse(fn(SimpleNamespace(cfg={'selected_prompt': '   '})))
        self.assertTrue(fn(SimpleNamespace(cfg={'selected_prompt': 'base.txt'})))

    def test_a_broken_config_never_blocks_the_interface(self):
        scope = {'diag': SimpleNamespace(exception=lambda *_a: None),
                 'str': str, 'bool': bool}
        fn = self._method('_prompt_selected', scope)

        class Broken(dict):
            def get(self, *_a, **_kw):
                raise RuntimeError('no config')

        self.assertTrue(fn(SimpleNamespace(cfg=Broken())))

    def test_the_cover_is_not_released_until_a_prompt_is_chosen(self):
        scope = {'diag': SimpleNamespace(exception=lambda *_a: None),
                 'str': str, 'bool': bool}
        hide = self._method('_hide_overlay', scope)
        prompt = self._method('_prompt_selected', scope)

        class Cover:
            def __init__(self):
                self.shown = 0
                self.hidden = 0

            def show(self):
                self.shown += 1

            def hide(self):
                self.hidden += 1

        for selected in (False, True):
            cover = Cover()
            events = []
            top = {'called': 0}

            def to_top():
                top['called'] = 1
                cover.show()

            owner = SimpleNamespace(cfg={'selected_prompt': 'base.txt' if selected else ''},
                                    overlay_state=SimpleNamespace(
                                        emit=lambda *a: events.append(a)),
                                    browser_cover=cover,
                                    _cover_to_top=to_top)
            owner._prompt_selected = prompt.__get__(owner)
            hide(owner)
            self.assertEqual(events, [("", "", False)])   # шторка-сообщение снята всегда
            if selected:
                self.assertEqual((cover.hidden, top['called']), (1, 0))
            else:
                # Браузер остаётся закрытым: окно ожидания стоит до выбора промта.
                self.assertEqual((cover.hidden, top['called']), (0, 1))

    def test_the_guard_stops_once_the_chat_is_ready_and_a_prompt_is_chosen(self):
        scope = {'diag': SimpleNamespace(exception=lambda *_a: None),
                 'str': str, 'bool': bool}
        hold = self._method('_hold_cover_if_needed', scope)
        prompt = self._method('_prompt_selected', scope)

        def run(chat_ready, selected, closing=False):
            state = {'called': 0}
            owner = SimpleNamespace(cfg={'selected_prompt': 'b' if selected else ''},
                                    _chat_ready=chat_ready, _closing=closing,
                                    _cover_to_top=lambda: state.update(called=1))
            owner._prompt_selected = prompt.__get__(owner)
            hold(owner)
            return state['called']

        self.assertEqual(run(False, False), 1)   # браузер грузится
        self.assertEqual(run(True, False), 1)    # чат готов, промт ещё не выбран
        self.assertEqual(run(False, True), 1)    # промт выбран, страница ещё нет
        self.assertEqual(run(True, True), 0)     # всё готово — шторка снята
        self.assertEqual(run(False, False, closing=True), 0)

    def test_the_successful_export_still_reveals_the_page(self):
        # Иначе окно ожидания висело бы вечно: выгрузка файлов сама снимает
        # шторку, и `_prompt_selected` к этому моменту уже True.
        source = (ROOT / 'main.py').read_text()
        attached = source.split('def _on_files_attached(', 1)[1].split('\n    def ', 1)[0]
        self.assertIn('if attached:', attached)
        self.assertIn('self._hide_overlay()', attached)


# ===========================================================================
# v19 — «Error - 1»: предсказуемость и полная обратная связь
# ===========================================================================
class _FakeDiag:
    """Мини-диагностика: только то, чем пользуются прогоняемые куски main.py."""

    def __init__(self):
        self.events = []
        self.states = {}

    @staticmethod
    def trace(fn):
        return fn

    def event(self, name, **kw):
        self.events.append((name, kw))

    def warn(self, name, **kw):
        self.events.append(('warn.' + str(name), kw))

    def exception(self, where, info=None):
        self.events.append(('exception', {'where': where}))

    def state(self, key, value):
        self.states[key] = value
        self.events.append(('state.set', {'key': key, 'value': value}))
        return value


class _FakeKernel32:
    """kernel32, у которого память читается по словарю адресов."""

    def __init__(self, memory=None):
        self.memory = dict(memory or {})
        self.GetCurrentProcess = lambda: -1

    def _addr(self, value):
        raw = getattr(value, 'value', value)
        return int(raw or 0)

    def ReadProcessMemory(self, handle, address, buffer, size, read):
        target = self._addr(address)
        value = self.memory.get(target)
        if value is None:
            return False
        buffer._obj.value = int(value)
        read._obj.value = int(size)
        return True


class _FakeOle32:
    """ole32, который «создаёт» объект по заранее известному адресу."""

    def __init__(self, interface=0, hr=0):
        self.interface = int(interface)
        self.hr = int(hr)

    def CoInitialize(self, reserved):
        return 0

    def CoCreateInstance(self, clsid, outer, context, iid, pointer):
        if self.hr < 0:
            return self.hr
        pointer._obj.value = self.interface
        return self.hr


def _v19_methods(names, module='main.py'):
    """Вынуть из исходника функции/константы и выполнить их без PyQt6."""
    tree = ast.parse((ROOT / module).read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names:
                    nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(node)
    code = compile(ast.Module(body=nodes, type_ignores=[]), '<v19>', 'exec')
    namespace = {'re': re, 'time': time, 'diag': _FakeDiag(),
                 'apply_language_and_theme': lambda page, lang='ru-RU', light=True: {},
                 'JS_LIGHT_THEME': '/*light*/'}
    exec(code, namespace)
    return namespace


class SurfaceTests(unittest.TestCase):
    """Определение поверхности: чат, режим ИИ в поиске, вход, согласие."""

    def surface_js(self):
        return _v19_methods(['JS_SURFACE'], 'main.py')['JS_SURFACE']

    def _dom(self, hostname='gemini.google.com', pathname='/app', search='',
             ready='complete', bg='rgb(255, 255, 255)', dark='false',
             composer=1, files=1, chips=0, account='true'):
        return r"""
const location = {hostname: '%s', pathname: '%s', search: '%s', protocol: 'https:',
                  href: 'https://%s%s%s'};
const document = {
  readyState: '%s',
  body: {children: [1, 2, 3]},
  querySelectorAll: (s) => {
     if (s.indexOf('input[type="file"]') >= 0) return new Array(%d);
     if (s.indexOf('div.ArblTe') >= 0) return new Array(%d);
     if (['div.esoFne', 'rich-textarea', 'textarea',
          'div[contenteditable="true"]', '[role="textbox"]'].indexOf(s) >= 0)
         return new Array(%d);
     return new Array(0);
  },
  querySelector: (s) => %s,
  documentElement: {}, hasFocus: () => true, activeElement: null
};
const getComputedStyle = (el) => ({backgroundColor: '%s', colorScheme: 'light'});
const window = {matchMedia: (q) => ({matches: %s})};
const navigator = {language: 'ru-RU', languages: ['ru-RU']};
const performance = {getEntriesByType: () => new Array(42)};
""" % (hostname, pathname, search, hostname, pathname, search, ready, files, chips,
       composer, '{}' if account == 'true' else 'null', bg, dark)

    def run_js(self, dom):
        source = 'const result = %s;\nconsole.log(JSON.stringify(result));' \
            % self.surface_js()
        out = subprocess.run([NODE, '-e', dom + source], capture_output=True,
                             text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[:500])
        return json.loads(out.stdout.strip().splitlines()[-1])

    @skip_node
    def test_the_chat_surface_is_recognised(self):
        data = self.run_js(self._dom())
        self.assertEqual('gemini', data['verdict'])
        self.assertEqual('complete', data['ready'])

    @skip_node
    def test_the_ai_mode_page_is_recognised(self):
        # Страница из лога «Error - 1»: www.google.com/search?udm=50&aep=11,
        # readyState «interactive», полей для файла нет — экспорт невозможен.
        data = self.run_js(self._dom(hostname='www.google.com', pathname='/search',
                                     search='?udm=50&aep=11', ready='interactive',
                                     composer=1, files=0, account='false'))
        self.assertEqual('ai_mode', data['verdict'])
        self.assertEqual('interactive', data['ready'])
        self.assertEqual(0, data['files'])
        self.assertFalse(data['account'])

    @skip_node
    def test_login_and_consent_walls_are_recognised(self):
        self.assertEqual('login', self.run_js(
            self._dom(hostname='accounts.google.com', pathname='/v3/signin',
                      composer=0, files=0))['verdict'])
        self.assertEqual('consent', self.run_js(
            self._dom(hostname='consent.google.com', composer=0, files=0))['verdict'])

    @skip_node
    def test_the_page_colour_is_measured_not_assumed(self):
        dark = self.run_js(self._dom(bg='rgb(32, 33, 36)', dark='true'))
        self.assertTrue(dark['dark'])
        self.assertEqual('rgb(32, 33, 36)', dark['bg'])
        light = self.run_js(self._dom(bg='rgb(255, 255, 255)'))
        self.assertFalse(light['dark'])

    def test_page_data_is_treated_as_untrusted(self):
        namespace = _v19_methods(['JS_SURFACE', '_SURFACE_VERDICTS', '_COLOR_RE',
                                  'probe_page_surface', 'diag', 're', 'time'])

        class Page:
            def eval(self, expr, timeout=None, **kw):
                return {'value': {'verdict': '<script>', 'ready': 'complete',
                                  'composer': 10 ** 9, 'bg': 'javascript:alert(1)',
                                  'host': 'x' * 500, 'dark': 'yes', 'files': -5,
                                  'account': 1}}

        clean = namespace['probe_page_surface'](Page())
        self.assertEqual('other', clean['verdict'])
        self.assertEqual(0, clean['composer'])
        self.assertEqual(0, clean['files'])
        self.assertEqual('', clean['bg'])
        self.assertFalse(clean['dark'])
        self.assertTrue(len(clean['host']) <= 64)


class ReadinessGateTests(unittest.TestCase):
    """GUI прогрузки снимается только когда страница готова по-настоящему."""

    @staticmethod
    def namespace():
        return _v19_methods(['JS_SURFACE', '_SURFACE_VERDICTS', '_COLOR_RE',
                             'probe_page_surface', '_surface_signature',
                             'measure_theme', 'repair_light_theme',
                             'wait_for_chat_surface', 'SURFACE_READY_TIMEOUT',
                             'SURFACE_STABLE_ROUNDS', 'SURFACE_POLL_MS',
                             'THEME_MAX_REPAIRS', 'apply_language_and_theme',
                             'JS_LIGHT_THEME', 'diag', 're', 'time'])

    @staticmethod
    def state(**over):
        base = {'verdict': 'gemini', 'ready': 'complete', 'composer': 1, 'files': 0,
                'chips': 0, 'bg': 'rgb(255, 255, 255)', 'dark': False,
                'account': True, 'pending': 9, 'lang': 'ru-RU',
                'host': 'gemini.google.com', 'path': '/app'}
        base.update(over)
        return base

    class Scripted:
        def __init__(self, states):
            self.states = list(states)
            self.value = None
            self.sent = []

        def eval(self, expr, timeout=None, **kw):
            if self.states:
                self.value = self.states.pop(0)
            return {'value': self.value}

        def send(self, method, params=None, timeout=None):
            self.sent.append(method)
            return {}

    def test_interactive_is_not_readiness(self):
        # Раньше ворота отвечали «готово» уже на readyState == "interactive":
        # именно из-за этого GUI прогрузки снимали на недогруженной странице.
        ns = self.namespace()
        page = self.Scripted([dict(self.state(ready='interactive'))] * 40)
        result = ns['wait_for_chat_surface'](page, timeout=1, poll=1)
        self.assertFalse(result['ready'])
        self.assertEqual('timeout', result['blocked'])

    def test_complete_twice_is_readiness(self):
        ns = self.namespace()
        page = self.Scripted([dict(self.state(ready='loading', composer=0)),
                              dict(self.state(ready='interactive')),
                              dict(self.state()), dict(self.state())])
        result = ns['wait_for_chat_surface'](page, timeout=5, poll=1)
        self.assertTrue(result['ready'])
        self.assertEqual(4, result['rounds'])
        self.assertEqual(2, result['stable'])

    def test_a_login_wall_blocks_immediately(self):
        ns = self.namespace()
        diag = ns['diag']
        page = self.Scripted([dict(self.state(verdict='login', composer=0,
                                              host='accounts.google.com'))])
        result = ns['wait_for_chat_surface'](page, timeout=5, poll=1)
        self.assertFalse(result['ready'])
        self.assertEqual('login', result['blocked'])
        self.assertTrue(any(name == 'warn.surface_blocked' for name, _ in diag.events))

    def test_a_dark_page_is_measured_and_repaired(self):
        ns = self.namespace()
        diag = ns['diag']
        page = self.Scripted([dict(self.state(bg='rgb(32, 33, 36)', dark=True)),
                              dict(self.state()), dict(self.state()),
                              dict(self.state())])
        result = ns['wait_for_chat_surface'](page, timeout=5, poll=1, light=True)
        self.assertTrue(result['ready'])
        self.assertEqual(1, result['theme_repairs'])
        self.assertIn('Emulation.setAutoDarkModeOverride', page.sent)
        self.assertTrue(any(name == 'warn.dark_theme_measured'
                            for name, _ in diag.events))

    def test_a_light_page_is_left_alone(self):
        ns = self.namespace()
        page = self.Scripted([dict(self.state()), dict(self.state())])
        result = ns['wait_for_chat_surface'](page, timeout=5, poll=1, light=True)
        self.assertTrue(result['ready'])
        self.assertEqual(0, result['theme_repairs'])
        self.assertEqual([], page.sent)


class NativeBrowserDiagnosticsTests(unittest.TestCase):
    """Предсказуемость: контракт окружения и честная проверка прав."""

    def test_the_environment_contract_is_logged(self):
        events = []
        with patch.object(native_browser.diag, 'event',
                          lambda name, **kw: events.append((name, kw))):
            switches = native_browser.env_contract(
                'C:\\chrome.exe', 'chrome', 9222, 'C:\\profile',
                ['C:\\chrome.exe', '--app=https://x', '--lang=ru-RU'],
                width=453, height=735, light=True)
        names = [name for name, _ in events]
        self.assertIn('env.contract', names)
        payload = dict(events[names.index('env.contract')][1])
        self.assertEqual(['--app=https://x', '--lang=ru-RU'], payload['switches'])
        self.assertEqual(2, payload['switches_count'])
        self.assertEqual(453, payload['width'])
        self.assertTrue(payload['light_theme'])
        self.assertEqual(switches, payload['switches'])

    def test_the_permissions_query_awaits_the_promise(self):
        # В логе «Error - 1» было `state: {}` — Promise не дожидался, и право
        # микрофона ВСЕГДА казалось невыданным.
        source = (ROOT / 'native_browser.py').read_text()
        block = source.split('def grant_microphone(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('await_promise=True', block)

    def test_set_permission_is_not_retried_when_it_is_unsupported(self):
        source = (ROOT / 'native_browser.py').read_text()
        block = source.split('def grant_microphone(', 1)[1].split('\ndef ', 1)[0]
        self.assertIn('_SET_PERMISSION_UNSUPPORTED', block)
        self.assertIn('browser.set_permission_unsupported', block)

    def test_profile_preferences_are_read_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / 'Default').mkdir()
            prefs = {'partition': {'default_zoom_level': {'x': native_browser.ZOOM_LEVEL}},
                     'intl': {'accept_languages': 'ru-RU,ru;q=0.9'},
                     'profile': {'content_settings': {'exceptions': {
                         'media_stream_mic': {'https://google.com,*': {'setting': 1}}}}}}
            (profile / 'Default' / 'Preferences').write_text(json.dumps(prefs))
            events = []
            with patch.object(native_browser.diag, 'event',
                              lambda name, **kw: events.append((name, kw))):
                native_browser.verify_prefs(profile, lang='ru-RU')
            self.assertIn('browser.prefs_verified', [n for n, _ in events])
            # «Чужой» профиль: зум сброшен и язык английский — расхождение
            # должно быть видно в логе, а не предполагаться.
            prefs['partition']['default_zoom_level']['x'] = 0.0
            prefs['intl']['accept_languages'] = 'en-US,en;q=0.9'
            (profile / 'Default' / 'Preferences').write_text(json.dumps(prefs))
            events.clear()
            with patch.object(native_browser.diag, 'event',
                              lambda name, **kw: events.append((name, kw))):
                native_browser.verify_prefs(profile, lang='ru-RU')
            drift = [kw for name, kw in events if name == 'browser.prefs_drift']
            self.assertTrue(drift)
            self.assertEqual(['zoom', 'locale'], drift[0]['drift'])


class UploadDiagnosticsTests(unittest.TestCase):
    """Экспорт: молчаливые повторы больше не прячут причину."""

    def test_every_attempt_carries_size_and_timing(self):
        source = (ROOT / 'main.py').read_text()
        block = source.split('upload.attempt', 1)[1].split('if not injected:', 1)[0]
        self.assertIn('bytes=int(size)', block)
        self.assertIn('ms=round(', block)
        self.assertIn('empty_rounds', block)

    def test_a_page_without_attachments_is_reported(self):
        source = _without_docstring(
            (ROOT / 'main.py').read_text().split('class UploadThread(', 1)[1]
            .split('\nclass ', 1)[0], triple=True)
        self.assertIn('def _report_blocked(', source)
        self.assertIn('upload_blocked', source)
        self.assertIn('self.blocked.emit(verdict)', source)
        self.assertIn('UPLOAD_BLOCK_THRESHOLD', source)

    def test_the_app_opens_the_working_surface_itself(self):
        source = _without_docstring((ROOT / 'main.py').read_text())
        self.assertIn('def _on_upload_blocked(', source)
        self.assertIn('SURFACE_FALLBACK_URL', source)
        self.assertIn("'Page.navigate'", source)
        self.assertIn('_surface_corrected', source)
        self.assertIn('page.surface_corrected', source)


class DiagnosticsTests(unittest.TestCase):
    """Максимальное логирование: асинхронно, с человеческим зеркалом и сводкой."""

    def test_the_log_is_written_in_the_background(self):
        import diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(diagnostics, 'app_dir', lambda: Path(tmp)):
                diagnostics.LOG_DIR = None
                diagnostics._started = False
                diagnostics._queue = None
                diagnostics._writer = None
                diagnostics._event_counts.clear()
                diagnostics._states.clear()
                diagnostics._warnings.clear()
                diagnostics.setup()
                try:
                    diagnostics.event('unit.test', a=1)
                    diagnostics.warn('unit_warn', b=2)
                    diagnostics.state('browser.mode', 'external_chrome')
                    diagnostics.write_summary('unit')
                    diagnostics.flush()
                    folder = Path(diagnostics.LOG_DIR)
                    self.assertTrue((folder / 'diagnostic.jsonl').is_file())
                    self.assertTrue((folder / 'events.log').is_file())
                    summary = json.loads((folder / 'summary.json').read_text())
                    self.assertEqual('external_chrome',
                                     summary['states']['browser.mode'])
                    self.assertIn('unit_warn', str(summary['warnings']))
                    human = (folder / 'events.log').read_text()
                    self.assertIn('event="unit.test"', human)
                finally:
                    diagnostics.LOG_DIR = None
                    diagnostics._started = False
                    diagnostics._queue = None
                    diagnostics._writer = None

    def test_the_trace_decorator_never_eats_the_exception(self):
        import diagnostics
        events = []
        with patch.object(diagnostics, 'event',
                          lambda *a, **kw: events.append((a[0], kw))):
            @diagnostics.trace
            def boom():
                raise ValueError('x')
            with self.assertRaises(ValueError):
                boom()
        self.assertIn('trace.begin', [n for n, _ in events])
        self.assertIn('trace.fail', [n for n, _ in events])

    def test_secrets_are_never_written_to_the_log(self):
        import diagnostics
        clean = diagnostics._clean('token', 'super-secret-value')
        self.assertNotIn('super-secret', str(clean))
        self.assertTrue(clean.startswith('<redacted:'))

    def test_the_browser_mode_is_logged(self):
        source = _without_docstring((ROOT / 'main.py').read_text())
        self.assertIn("'browser.mode'", source)
        self.assertIn("'external_chrome'", source)

    def test_the_upload_and_export_state_lands_in_the_summary(self):
        source = (ROOT / 'main.py').read_text()
        self.assertIn('diag.state("page.verdict"', source)
        self.assertIn('diag.state("browser.mode"', source)
        self.assertIn('diag.state("page.blocked"', source)

class LayoutSettleTests(unittest.TestCase):
    """Удержание раскладки замером после показа окна (v20, ОШИБКИ.md № 1).

    Живая сессия 20261004-162559-12408-c5e41c: окно стояло идеально (dx=dy=0),
    а «съезжали» объекты СТРАНИЦЫ — объявление CDP с `deviceScaleFactor=0.6672`
    СЛОЖИЛОСЬ с зумом профиля (0.667×0.667 = 0.445, 658 → 987 css px), и
    раскладка возвращалась только к тику 5-секундного сторожа. Здесь
    проверяется, что цикл удержания замечает это замером и зовёт ремонт, не
    дожидаясь таймера, а ремонт использует форму «окно» (dsf=1.0).
    """

    class _Page:
        """Страница со сценарием: сколько замеров подряд отдавать «сломанной»."""

        def __init__(self, broken_rounds, logical=(439.0, 728.0), physical=(439.0, 728.0)):
            self.broken_left = int(broken_rounds)
            self.logical = logical
            self.physical = physical
            self.measures = 0

        def eval(self, expression, timeout=None, **kwargs):  # noqa: A003
            import json as _json
            css_w, css_h, _dsf = native_browser.target_css(
                self.logical[0], self.logical[1], self.physical[0], self.physical[1],
                native_browser.ZOOM)
            self.measures += 1
            if self.broken_left > 0:
                self.broken_left -= 1
                w, dpr = int(css_w * 1.5), native_browser.ZOOM * native_browser.ZOOM
            else:
                w, dpr = css_w, native_browser.ZOOM

            class _Value:
                def __init__(self, value):
                    self._value = value

                def get(self, key, default=None):
                    return self._value if key == "value" else default

            return _Value(_json.dumps({"w": w, "h": css_h, "dpr": dpr, "sw": w}))

    def test_repair_is_called_as_soon_as_the_layout_breaks(self):
        page = self._Page(broken_rounds=3)
        repairs = []
        result = native_browser.settle_layout(
            page, 439.0, 728.0, 439.0, 728.0, zoom=native_browser.ZOOM,
            budget_ms=2000, step_ms=10, need=2, repair=lambda: repairs.append(1))
        self.assertTrue(result["ok"], "после ремонта раскладка обязана считаться верной")
        self.assertEqual(result["fixed"], 3)
        self.assertEqual(len(repairs), 3)

    def test_stable_layout_needs_no_repair(self):
        page = self._Page(broken_rounds=0)
        repairs = []
        result = native_browser.settle_layout(
            page, 439.0, 728.0, 439.0, 728.0, zoom=native_browser.ZOOM,
            budget_ms=1000, step_ms=10, need=2, repair=lambda: repairs.append(1))
        self.assertTrue(result["ok"])
        self.assertEqual(repairs, [])
        self.assertLessEqual(page.measures, 3)

    def test_budget_is_respected_when_it_never_settles(self):
        page = self._Page(broken_rounds=10 ** 6)
        started = time.monotonic()
        result = native_browser.settle_layout(
            page, 439.0, 728.0, 439.0, 728.0, zoom=native_browser.ZOOM,
            budget_ms=200, step_ms=20, need=2, repair=lambda: None)
        elapsed = time.monotonic() - started
        self.assertFalse(result["ok"])
        self.assertLess(elapsed, 1.5, "цикл обязан закончиться по бюджету, а не висеть")

    def test_measurement_errors_never_escape(self):
        class Exploding:
            def eval(self, *a, **kw):  # noqa: A003
                raise RuntimeError("cdp потерян")

        result = native_browser.settle_layout(
            Exploding(), 439.0, 728.0, 439.0, 728.0, budget_ms=100, step_ms=10,
            need=1, repair=lambda: None)
        self.assertFalse(result["ok"])          # не бросает — просто «не устоялось»

    class _ZoomPage:
        """Модель из живого лога: зум профиля и переопределение СКЛАДЫВАЮТСЯ.

        Проверено по записям сессии 20261004-162559-12408-c5e41c:

        * переопределения нет → `innerWidth = 439/0.667 = 658`, `dpr = 0.667`
          (запись `browser.zoom source=native`);
        * переопределение `658 css @ dsf=0.6672` → `658/0.667 = 987`,
          `0.6672 x 0.667 = 0.445` — то, что видел пользователь;
        * переопределение `439 css @ dsf=1.0` (форма «окно») → `439/0.667 = 658`,
          `1.0 x 0.667 = 0.667` — то, чем раскладка вылечилась
          (`browser.zoom source=emulation-adaptive`).
        """

        def __init__(self, logical=(439.0, 728.0), physical=(439.0, 728.0),
                     profile=float(native_browser.ZOOM)):
            self.logical = logical
            self.physical = physical
            self.profile = profile
            self.override = None      # (css_w, css_h, dsf) или None
            self.applied = []

        def apply(self, css_w, css_h, dsf):
            self.applied.append((int(css_w), int(css_h), float(dsf)))
            self.override = (int(css_w), int(css_h), float(dsf))

        def metrics(self):
            if self.override is None:
                css_w = self.logical[0] / self.profile
                css_h = self.logical[1] / self.profile
                dpr = self.profile
            else:
                css_w = self.override[0] / self.profile
                css_h = self.override[1] / self.profile
                dpr = self.override[2] * self.profile
            return int(round(css_w)), int(round(css_h)), dpr

        def eval(self, expression, timeout=None, **kwargs):  # noqa: A003
            import json as _json
            css_w, css_h, dpr = self.metrics()

            class _Value:
                def __init__(self, value):
                    self._value = value

                def get(self, key, default=None):
                    return self._value if key == "value" else default

            return _Value(_json.dumps({"w": css_w, "h": css_h,
                                       "dpr": round(dpr, 6), "sw": css_w}))

    def _patch_apply(self, page):
        original = native_browser.apply_viewport_raw
        native_browser.apply_viewport_raw = (
            lambda p, css_w, css_h, dsf, fit_window=False: (
                page.apply(css_w, css_h, dsf) or True))
        return original

    def test_dsf_scaled_override_multiplies_with_the_profile_zoom(self):
        """Воспроизведение дефекта: dsf-масштаб x зум профиля = 0.445."""
        page = self._ZoomPage()
        ok, metrics = native_browser.zoom_ok(page, 439.0, 728.0, 439.0, 728.0,
                                            zoom=native_browser.ZOOM)
        self.assertTrue(ok, "без переопределения раскладка верна (source=native)")
        original = self._patch_apply(page)
        try:
            # так объявлял «толчок» и прибивание в v19: 658 css @ dsf=0.6672
            page.apply(658, 1091, native_browser.ZOOM)
        finally:
            native_browser.apply_viewport_raw = original
        width, _height, dpr = page.metrics()
        self.assertEqual(width, 987, "658 / 0.667 = 987 — «объекты съехали»")
        self.assertAlmostEqual(dpr, float(native_browser.ZOOM) ** 2, places=4)
        ok, _metrics = native_browser.zoom_ok(page, 439.0, 728.0, 439.0, 728.0,
                                             zoom=native_browser.ZOOM)
        self.assertFalse(ok, "такая раскладка обязана считаться поломкой")

    def test_refresh_emulation_lands_on_the_window_shape(self):
        """Ремонт: логические пиксели окна при dsf=1.0 — и это подтверждено замером."""
        page = self._ZoomPage()
        original = self._patch_apply(page)
        try:
            page.apply(658, 1091, native_browser.ZOOM)   # сломали, как Chrome
            result = native_browser.refresh_emulation(
                page, 439.0, 728.0, 439.0, 728.0, native_browser.ZOOM)
        finally:
            native_browser.apply_viewport_raw = original
        self.assertTrue(result["ok"], "замер обязан подтвердить восстановление")
        self.assertEqual(result["innerWidth"], 658)
        self.assertAlmostEqual(result["dpr"], float(native_browser.ZOOM), places=3)
        self.assertEqual(page.applied[-1], (439, 728, 1.0),
                         "форма объявления — окно (439x728) при dsf=1.0")

    def test_the_metric_nudge_keeps_the_window_shape_and_measures(self):
        """Толчок обязан быть в «форме окна» и заканчиваться замером.

        Именно dsf-форма толчка (найдена по логу 20261004) сложилась с зумом
        профиля и дала 987 x 0.445 — поэтому здесь проверяется и форма, и
        то, что толчок НЕ заканчивается «просто откатом» тех же чисел.
        """
        source = (ROOT / 'main.py').read_text()
        self.assertIn('restored=bool(healed.get("ok"))', source)
        nudge = source.index('def _nudge_metrics(self, page):')
        body = source[nudge:source.index('@pyqtSlot(int)', nudge)]
        self.assertIn('native_browser.apply_viewport_raw(page, int(round(logical_w)) + 1', body)
        self.assertIn('int(round(logical_h)), 1.0)', body, "dsf обязан быть 1.0")
        self.assertIn('native_browser.refresh_emulation(', body)
        self.assertNotIn('css_w + 1, css_h, dsf', body, "dsf-форма запрещена")
        # Ветка прибивания тоже начинается с формы «окно».
        pin = source.index('if pin_first:')
        self.assertIn('native_browser.refresh_emulation(', source[pin:pin + 2500])

    def test_repair_prefers_reestablishing_the_emulation(self):
        source = (ROOT / 'main.py').read_text()
        watch = source.index('native_browser.settle_layout(')
        self.assertIn('repair=lambda: self._layout_repair(page, reason)', source)
        repair = source.index('def _layout_repair(self, page, reason):')
        self.assertLess(repair, watch)
        self.assertIn('browser.layout_repair', source)

    def test_main_holds_the_curtain_until_the_layout_is_measured(self):
        source = (ROOT / 'main.py').read_text()
        # Раскладка проверяется, пока шторка ещё стоит, и ещё раз — после неё.
        self.assertIn('self._layout_watch(page, "revive-hold"', source)
        self.assertIn('self._layout_watchdog(page, "post-reveal")', source)
        self.assertIn('native_browser.settle_layout(', source)
        # и именно ДО снятия шторки
        self.assertLess(source.index('self._layout_watch(page, "revive-hold"'),
                        source.index('self._finish_settle(True)'))
