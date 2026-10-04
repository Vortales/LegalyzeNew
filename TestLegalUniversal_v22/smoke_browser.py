"""Windows pre-check without Google login, server auth or network access.

Loads a local test page that imitates the chat editor and microphone button,
then verifies: page readiness gate, microphone discovery/click, and the Web
Speech API surface (native or the Legalyze shim). Exit code 0 = all passed.
Run: .venv\\Scripts\\python.exe smoke_browser.py
"""
import ast
import json
import sys
from pathlib import Path

from PyQt6.QtCore import QUrl, QTimer
from PyQt6.QtWidgets import QApplication
from qt_browser import BrowserPane, READY_SCRIPT, ZOOM_FACTOR
from web_compat import CAPABILITY_JS
from storage_paths import app_dir
import diagnostics as diag

ROOT = Path(__file__).resolve().parent


def _js_constant(name):
    tree = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise KeyError(name)


TEST_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>smoke</title></head>
<body>
<header id="gb">unneeded chrome</header>
<div class="Txyg0d" role="region">
  <textarea aria-label="Задайте вопрос"></textarea>
  <button data-xid="input-plate-voice-button" aria-label="Микрофон"
          onclick="window.__micClicks=(window.__micClicks||0)+1">mic</button>
  <button data-xid="input-plate-voice-send-button" aria-label="Отправить">send</button>
</div>
<input type="file" accept="application/pdf">
</body></html>"""

STEPS = [
    ('ready', READY_SCRIPT, lambda v: v is True),
    ('mic', _js_constant('JS_START_RECORDING'),
     lambda v: isinstance(v, dict) and v.get('ok') is True and 'x' in v),
    # Клик перехватывается: обработчик сайта (getUserMedia) не вызывается,
    # запись ведёт настоящая диктовка через наш мост.
    ('mic_site_blocked', 'window.__micClicks || 0', lambda v: v == 0),
    ('mic_dictation', '!!(window.__legalyzeMic && window.__legalyzeMic.active())',
     lambda v: v is True),
    ('speech', "typeof (window.SpeechRecognition || window.webkitSpeechRecognition)",
     lambda v: v == 'function'),
    ('caps', CAPABILITY_JS, lambda v: isinstance(v, dict) and v.get('mediaDevices') is True),
    ('mic_replica', """(() => {
      const native = document.querySelector('button[data-xid="input-plate-voice-button"]');
      if (native) native.remove();
      if (window.__legalyzeMic) window.__legalyzeMic.ensure();
      const rep = document.getElementById('legalyze-mic');
      return !!(rep && rep.getAttribute('data-xid') === 'input-plate-voice-button'
                && window.__legalyzeMic && window.__legalyzeDictate);
    })()""", lambda v: v is True),
    ('mic_stop', 'window.__legalyzeMic && window.__legalyzeMic.stop(); true',
     lambda v: v is True),
]


def main():
    diag.setup()
    app = QApplication(sys.argv)
    pane = BrowserPane(app_dir() / 'qtwebengine-smoke-profile')
    pane.resize(480, 640)
    pane.show()
    results = {}

    def fail(message):
        print('SMOKE FAIL:', message)
        app.quit()

    def finish():
        for name, ok in results.items():
            print(('PASS ' if ok else 'FAIL ') + name)
        ok = all(results.values()) and len(results) == len(STEPS) + 1
        print('SMOKE', 'OK' if ok else 'FAILED')
        app.quit()
        pane.shutdown()
        sys.exit(0 if ok else 1)

    def step(index):
        if index >= len(STEPS):
            finish()
            return
        name, script, check = STEPS[index]

        def done(value):
            try:
                results[name] = bool(check(value))
            except Exception as exc:
                results[name] = False
                print('step error', name, exc)
            QTimer.singleShot(0, lambda: step(index + 1))

        pane.page.runJavaScript(script, done)

    def loaded(ok):
        if not ok:
            fail('local test page did not load')
            return
        QTimer.singleShot(300, lambda: step(0))

    pane.view.loadFinished.connect(loaded)
    results['zoom_native_67'] = abs(float(pane.view.zoomFactor()) - ZOOM_FACTOR) < 0.001
    pane.view.setHtml(TEST_HTML, QUrl('https://google.com/ai'))
    QTimer.singleShot(30000, lambda: fail('timeout'))
    app.exec()
    sys.exit(1)


if __name__ == '__main__':
    main()
