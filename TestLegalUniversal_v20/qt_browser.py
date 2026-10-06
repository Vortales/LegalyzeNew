"""Native Qt browser widget with speech bridge and page-compatibility layer."""
import os
import re
import socket
from pathlib import Path

# A per-process port; never expose DevTools on LAN. No sandbox-disabling flags.
with socket.socket() as listener:
    listener.bind(('127.0.0.1', 0))
    DEBUG_PORT = listener.getsockname()[1]
os.environ['QTWEBENGINE_REMOTE_DEBUGGING'] = f'127.0.0.1:{DEBUG_PORT}'

from PyQt6.QtCore import QUrl, QCoreApplication, Qt, QObject, QFile, pyqtSignal, pyqtSlot
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                   QWebEngineScript, QWebEnginePermission)
from PyQt6.QtWebEngineWidgets import QWebEngineView
import diagnostics as diag
from speech_backend import DictationEngine
from web_compat import (UA_BRAND_JS, SPEECH_SHIM_JS, CHANNEL_INIT_JS, CAPABILITY_JS,
                        JS_PURGE, CONSENT_AUTOCLICK_JS, QWEBCHANNEL_LITE_JS, MIC_BUTTON_JS)

HOME = 'https://google.com/ai'
ALLOWED_HOSTS = {'google.com', 'www.google.com', 'gemini.google.com'}
# Native page zoom, identical to Chrome's "67%" step (2/3) used before.
ZOOM_FACTOR = 2.0 / 3.0
READY_SCRIPT = """(() => {
    if (location.protocol !== 'https:' ||
        !['google.com','www.google.com','gemini.google.com'].includes(location.hostname)) return false;
    if (!['interactive','complete'].includes(document.readyState)) return false;
    return Array.from(document.querySelectorAll('textarea,[contenteditable="true"],[role="textbox"]'))
        .some(e => e.getClientRects().length > 0 && !e.disabled && !e.readOnly);
})()"""


def load_qwebchannel_js():
    """Official Qt qwebchannel.js from the resource system, else the lite client."""
    try:
        f = QFile(':/qtwebchannel/qwebchannel.js')
        if f.open(QFile.OpenModeFlag.ReadOnly):
            data = bytes(f.readAll())
            f.close()
            if data:
                return data.decode('utf-8')
    except Exception:
        diag.exception('qt_browser.qwebchannel_resource')
    try:
        import PyQt6
        roots = [Path(PyQt6.__file__).parent]
        for pattern in ('Qt6/qml/QtWebChannel/qwebchannel.js',
                        'Qt5/qml/QtWebChannel/qwebchannel.js',
                        'Qt6/resources/qwebchannel/qwebchannel.js'):
            for root in roots:
                candidate = root / pattern
                if candidate.is_file():
                    return candidate.read_text(encoding='utf-8')
    except Exception:
        diag.exception('qt_browser.qwebchannel_file')
    return QWEBCHANNEL_LITE_JS


class SpeechBridge(QObject):
    """QWebChannel object: page-side SpeechRecognition shim <-> Windows dictation."""

    started = pyqtSignal()
    speechStart = pyqtSignal()
    speechEnd = pyqtSignal()
    hypothesis = pyqtSignal(str)
    result = pyqtSignal(str, float)
    rejected = pyqtSignal(str)
    error = pyqtSignal(str)
    ended = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.engine = DictationEngine(self._on_engine_event)

    @pyqtSlot(str, bool, bool, result=str)
    def start(self, lang, continuous, interim):
        diag.event('speech.request', lang=str(lang)[:16], continuous=bool(continuous),
                   interim=bool(interim))
        self.engine.start(str(lang or 'ru-RU'))
        return 'ok'

    @pyqtSlot()
    def stop(self):
        diag.event('speech.stop')
        self.engine.stop()

    @pyqtSlot()
    def abort(self):
        diag.event('speech.abort')
        self.engine.stop()

    @pyqtSlot(str)
    def reportCapabilities(self, raw):
        caps = str(raw or '')[:500]
        diag.event('webengine.speech_surface', caps=caps)

    def _on_engine_event(self, kind, data):
        try:
            if kind == 'started':
                self.started.emit()
            elif kind == 'speechstart':
                self.speechStart.emit()
            elif kind == 'speechend':
                self.speechEnd.emit()
            elif kind == 'hypothesis':
                self.hypothesis.emit(str(data.get('text') or ''))
            elif kind == 'result':
                self.result.emit(str(data.get('text') or ''),
                                 float(data.get('confidence') or 0.0))
            elif kind == 'rejected':
                self.rejected.emit(str(data.get('text') or ''))
            elif kind == 'error':
                self.error.emit(str(data.get('code') or 'network'))
            elif kind == 'end':
                self.ended.emit()
        except Exception:
            diag.exception('qt_browser.speech_event')

    def shutdown(self):
        try:
            self.engine.shutdown()
        except Exception:
            diag.exception('qt_browser.speech_shutdown')


class BrowserPane(QWidget):
    def __init__(self, profile_path, parent=None):
        super().__init__(parent)
        root = Path(profile_path)
        root.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile('LegalyzeQtWebEngine', self)
        self.profile.setPersistentStoragePath(str(root / 'storage'))
        self.profile.setCachePath(str(root / 'cache'))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies)
        self._normalize_user_agent()
        self.crashed = False
        self.crash_count = 0
        self.view = QWebEngineView(self)
        self.view.setZoomFactor(ZOOM_FACTOR)  # before the first load: no zoom flash
        self.page = QWebEnginePage(self.profile, self.view)
        self.view.setPage(self.page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.page.permissionRequested.connect(self._permission)
        # Login popups stay in the same managed view instead of orphan native windows.
        self.page.newWindowRequested.connect(lambda request: request.openIn(self.page))
        self.page.renderProcessTerminated.connect(self._terminated)
        self.view.loadFinished.connect(self._load_finished)
        self.view.loadStarted.connect(lambda: self.view.setZoomFactor(ZOOM_FACTOR))
        self._setup_speech_bridge()
        self._install_page_scripts()
        for shortcut, callback in [('Ctrl+R', self.view.reload), ('F5', self.view.reload),
                                   ('Alt+Left', self.view.back), ('Ctrl+Shift+H', self.open_home)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, fn=callback: fn())
            self.addAction(action)
        diag.event('webengine.created', port=DEBUG_PORT, profile=str(root))

    # ----------------------------------------------------------- compatibility

    def _normalize_user_agent(self):
        """Drop the embedder token so UA-based feature gating matches Chrome."""
        try:
            ua = self.profile.httpUserAgent()
            clean = re.sub(r'\s*QtWebEngine/[\d.]+', '', ua)
            if clean != ua:
                self.profile.setHttpUserAgent(clean)
                diag.event('webengine.ua_normalized')
        except Exception:
            diag.exception('qt_browser.ua')

    def _setup_speech_bridge(self):
        self.speech_bridge = SpeechBridge(self)
        self.channel = QWebChannel(self)
        self.channel.registerObject('legalyzeSpeech', self.speech_bridge)
        self.page.setWebChannel(self.channel)

    def _install_page_scripts(self):
        # Everything runs at document creation: no flash of unhidden chrome,
        # zoom already applied, speech surface present before site scripts.
        chunks = [UA_BRAND_JS, SPEECH_SHIM_JS, CONSENT_AUTOCLICK_JS, MIC_BUTTON_JS, JS_PURGE]
        channel_js = load_qwebchannel_js()
        chunks.append(channel_js)
        chunks.append(CHANNEL_INIT_JS)
        if channel_js is QWEBCHANNEL_LITE_JS:
            diag.event('webengine.qwebchannel_lite')
        else:
            diag.event('webengine.qwebchannel_ok')
        # ';'-separated statements: a chunk ending in ')' must not be parsed as
        # a call continuation of the next chunk starting with '(' (ASI hazard).
        source = ';\n'.join(chunks)
        script = QWebEngineScript()
        script.setName('legalyze-compat')
        script.setSourceCode(source)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        self.page.scripts().insert(script)

    def _load_finished(self, ok):
        diag.event('webengine.load_finished', success=bool(ok))
        if ok:
            try:
                self.page.runJavaScript(CAPABILITY_JS, self._caps_done)
            except Exception:
                diag.exception('qt_browser.caps')

    def _caps_done(self, value):
        try:
            if isinstance(value, dict):
                diag.event('webengine.capabilities',
                           **{k: value[k] for k in
                              ('secure', 'mediaDevices', 'mediaRecorder', 'speechNative',
                               'speechShim', 'uaPatched') if isinstance(value.get(k), bool)},
                           brands=str(value.get('brands') or '')[:120])
        except Exception:
            diag.exception('qt_browser.caps_done')

    # ----------------------------------------------------------------- policy

    def _permission(self, permission):
        origin = permission.origin()
        host = origin.host()
        allowed = (origin.scheme() == 'https' and
                   (host in ALLOWED_HOSTS or host == QUrl(HOME).host()) and
                   permission.permissionType() == QWebEnginePermission.PermissionType.MediaAudioCapture)
        if allowed:
            permission.grant()
        else:
            permission.deny()
        diag.event('webengine.permission', granted=allowed)

    def _terminated(self, status, code):
        # Renderer death (e.g. a site getUserMedia call on a build without
        # WebRTC) is reported to the host so it can cover the dead view and
        # auto-recover instead of leaving a blank page.
        self.crashed = True
        self.crash_count += 1
        diag.event('webengine.render_terminated', code=code, status=str(status),
                   count=self.crash_count)
        self.view.loadFinished.emit(False)

    def open_home(self):
        self.view.setUrl(QUrl(HOME))

    def shutdown(self):
        self.view.stop()
        # Delete the page before its non-default profile; flush cookies/storage via Qt teardown.
        from PyQt6 import sip
        try:
            self.speech_bridge.shutdown()
        except Exception:
            diag.exception('qt_browser.shutdown_speech')
        sip.delete(self.page)
        sip.delete(self.view)
        sip.delete(self.profile)
        self.deleteLater()
