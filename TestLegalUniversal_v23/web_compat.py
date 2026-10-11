"""Early page-compatibility scripts for the embedded QtWebEngine.

The site hides its microphone button when the Web Speech API is missing. In
real Chrome `SpeechRecognition` is a Google cloud service tied to Chrome's
proprietary keys; QtWebEngine does not ship it. `SPEECH_SHIM_JS` defines a
faithful SpeechRecognition surface backed (through QWebChannel, which is not
subject to page CSP) by the Windows dictation engine in `speech_backend.py`,
so the site renders its microphone and dictation works for real.

`UA_BRAND_JS` makes JS-side client hints consistent with the Chrome-branded
UA we send over the network, so feature gating by browser brand behaves the
same as in Chrome. Both scripts run at document creation, before any site
script, and are purely additive.
"""

# ---------------------------------------------------------------------------
UA_BRAND_JS = r"""
(() => {
  try {
    const m = /Chrome\/(\d+)/.exec(navigator.userAgent || '');
    const major = m ? m[1] : '120';
    const current = navigator.userAgentData;
    const brands = (current && current.brands) ? current.brands.map(b => b.brand) : [];
    if (current && brands.indexOf('Google Chrome') !== -1) return;
    const list = [
      { brand: 'Chromium', version: major },
      { brand: 'Google Chrome', version: major },
      { brand: 'Not;A=Brand', version: '24' }
    ];
    const data = {
      brands: list,
      mobile: false,
      platform: 'Windows',
      getHighEntropyValues: (hints) => Promise.resolve({
        brands: list, mobile: false, platform: 'Windows',
        platformVersion: '15.0.0', architecture: 'x86', bitness: '64',
        model: '', uaFullVersion: major + '.0.0.0', fullVersionList: list,
        wow64: false
      }),
      toJSON() { return { brands: list, mobile: false, platform: 'Windows' }; }
    };
    Object.defineProperty(navigator, 'userAgentData', {
      get: () => data, configurable: true
    });
    window.__legalyzeUaPatched = true;
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
SPEECH_SHIM_JS = r"""
(() => {
  try {
    if (window.__legalyzeSpeechShimInstalled) return;
    window.__legalyzeSpeechShimInstalled = true;
    // ВАЖНО: нативный SpeechRecognition в этой поставке QtWebEngine (Chromium
    // без Google API-ключа) РОНЯЕТ рендер-процесс при start() — проверено
    // логом (render_terminated CrashedTerminationStatus через мс после
    // rec.start()). Поэтому НИКОГДА не используем нативный класс: сохраняем
    // его под именем для диагностики и всегда ставим НАШ (QWebChannel +
    // Windows System.Speech) поверх window.SpeechRecognition.
    try {
      if (!window.__legalyzeNativeSpeechRecognition) {
        window.__legalyzeNativeSpeechRecognition =
          window.SpeechRecognition || window.webkitSpeechRecognition || null;
      }
    } catch (e) {}

    const waiters = [];
    const bridgeReady = (cb) => {
      if (window.__legalyzeSpeechBridge) { cb(window.__legalyzeSpeechBridge); return; }
      waiters.push(cb);
      setTimeout(() => {
        for (let i = 0; i < waiters.length; i++) {
          try { waiters[i](null); } catch (e) {}
        }
        waiters.length = 0;
      }, window.__legalyzeSpeechBridgeWait || 4000);
    };
    window.__legalyzeSpeechWaiters = waiters;

    class SpeechRecognitionAlternative {
      constructor(transcript, confidence) {
        this.transcript = transcript; this.confidence = confidence;
      }
    }
    class SpeechRecognitionResult extends Array {
      constructor(alternative, isFinal) {
        super(alternative);
        this.isFinal = isFinal;
      }
    }
    class SpeechRecognitionEvent {
      constructor(type, items, resultIndex) {
        this.type = type; this.resultIndex = resultIndex;
        this.results = items.map((it) =>
          new SpeechRecognitionResult(
            new SpeechRecognitionAlternative(it.transcript, it.confidence), it.final));
      }
    }
    class SpeechRecognitionErrorEvent {
      constructor(error) { this.type = 'error'; this.error = error; }
    }

    class SpeechRecognition {
      constructor() {
        this.lang = (navigator.language || 'ru-RU');
        this.continuous = false;
        this.interimResults = false;
        this.maxAlternatives = 1;
        this.serviceURI = '';
        this.grammars = null;
        this.onaudiostart = null; this.onaudioend = null;
        this.onspeechstart = null; this.onspeechend = null;
        this.onstart = null; this.onend = null;
        this.onresult = null; this.onerror = null; this.onnomatch = null;
        this._items = []; this._active = false; this._stopping = false;
      }

      _emit(name, ev) {
        const fn = this['on' + name];
        if (typeof fn === 'function') { try { fn.call(this, ev); } catch (e) {} }
      }

      _pushResult(text, isFinal, confidence) {
        const interimIndex = this._items.findIndex((it) => !it.final);
        const item = { transcript: text, confidence: confidence || 0.9, final: isFinal };
        if (interimIndex >= 0 && !isFinal) {
          this._items[interimIndex] = item;
          return interimIndex;
        }
        if (interimIndex >= 0 && isFinal) {
          this._items[interimIndex] = item;
          return interimIndex;
        }
        this._items.push(item);
        return this._items.length - 1;
      }

      _dispatchResult(text, isFinal, confidence) {
        if (!isFinal && !this.interimResults) return;
        const idx = this._pushResult(text, isFinal, confidence);
        this._emit('result', new SpeechRecognitionEvent('result', this._items, idx));
        if (isFinal && !this.continuous && !this._stopping) {
          this._stopping = true;
          bridgeReady((b) => { if (b) { try { b.stop(); } catch (e) {} } });
        }
      }

      start() {
        if (this._active) {
          const err = new Error('already started');
          err.name = 'InvalidStateError';
          throw err;
        }
        this._active = true;
        this._stopping = false;
        this._items = [];
        window.__legalyzeSpeechActive = this;
        bridgeReady((bridge) => {
          if (!bridge) {
            this._failed = true;
            this._emit('error', new SpeechRecognitionErrorEvent('network'));
            this._finish();
            return;
          }
          try {
            bridge.start(String(this.lang || 'ru-RU'), !!this.continuous, !!this.interimResults);
          } catch (e) {
            this._emit('error', new SpeechRecognitionErrorEvent('network'));
            this._finish();
          }
        });
      }

      stop() {
        if (!this._active) return;
        this._stopping = true;
        bridgeReady((b) => { if (b) { try { b.stop(); } catch (e) {} } });
      }

      abort() {
        if (!this._active) return;
        this._stopping = true;
        this._items = [];
        bridgeReady((b) => { if (b) { try { b.abort(); } catch (e) {} } });
        this._emit('error', new SpeechRecognitionErrorEvent('aborted'));
        this._finish();
      }

      _finish() {
        if (!this._active) return;
        this._active = false;
        if (window.__legalyzeSpeechActive === this) window.__legalyzeSpeechActive = null;
        this._emit('audioend', {});
        this._emit('end', {});
      }

      // ---- bridge events (called by the QWebChannel dispatch below) --------
      _bridgeEvent(kind, data) {
        if (!this._active) return;
        const d = data || {};
        if (kind === 'started') {
          this._emit('audiostart', {});
          this._emit('start', {});
        } else if (kind === 'speechstart') {
          this._emit('speechstart', {});
        } else if (kind === 'speechend') {
          this._emit('speechend', {});
        } else if (kind === 'hypothesis') {
          this._dispatchResult(String(d.text || ''), false, 0.0);
        } else if (kind === 'result') {
          this._dispatchResult(String(d.text || ''), true,
                               Number(d.confidence || 0.9));
        } else if (kind === 'rejected') {
          this._emit('nomatch', {});
        } else if (kind === 'error') {
          this._emit('error', new SpeechRecognitionErrorEvent(String(d.code || 'network')));
        } else if (kind === 'end') {
          this._finish();
        }
      }
    }

    window.LegalyzeSpeechRecognition = SpeechRecognition;
    window.SpeechRecognition = window.webkitSpeechRecognition = SpeechRecognition;
    window.__legalyzeSpeech = { mode: 'shim', SpeechRecognition };

    const dispatch = (kind, data) => {
      const inst = window.__legalyzeSpeechActive;
      if (inst) inst._bridgeEvent(kind, data || {});
    };
    window.__legalyzeSpeechDispatch = dispatch;

    bridgeReady((bridge) => {
      if (!bridge) return;
      bridge.started.connect(() => dispatch('started'));
      bridge.speechStart.connect(() => dispatch('speechstart'));
      bridge.speechEnd.connect(() => dispatch('speechend'));
      bridge.hypothesis.connect((text) => dispatch('hypothesis', { text: text }));
      bridge.result.connect((text, confidence) =>
        dispatch('result', { text: text, confidence: confidence }));
      bridge.rejected.connect(() => dispatch('rejected'));
      bridge.error.connect((code) => dispatch('error', { code: code }));
      bridge.ended.connect(() => dispatch('end'));
    });
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
# Accepted-consent clicker: Google shows cookie/consent cards on a fresh
# profile. Hiding them without accepting leaves the page half-blocked (the
# first-ever run then "spins forever" on a query). Only exact accept labels
# inside consent surfaces are clicked.
CONSENT_AUTOCLICK_JS = r"""
(() => {
  try {
    if (window.__legalyzeConsentInstalled) return;
    window.__legalyzeConsentInstalled = true;
    const ACCEPT = /^(принять все|принять|accept all|accept|i agree|agree|согласен|согласиться|подтвердить)$/i;
    const SURFACE = 'div[role="dialog"], .qEn1od, form[action*="consent"], div.lJwFBd, div[data-consent], div.tDoycf';
    const clickAccepts = () => {
      try {
        if (!document.body) return 0;
        const onConsentHost = /(^|\.)consent\.google\.com$/.test(location.hostname || '');
        let clicked = 0;
        const nodes = document.querySelectorAll('button, input[type="submit"], div[role="button"]');
        for (const el of nodes) {
          const label = String(el.getAttribute('aria-label') || el.innerText || el.value || '').trim();
          if (!ACCEPT.test(label)) continue;
          if (!onConsentHost && !(el.closest && el.closest(SURFACE))) continue;
          try { el.click(); clicked++; } catch (e) {}
        }
        return clicked;
      } catch (e) { return 0; }
    };
    window.__legalyzeConsent = clickAccepts;
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      clickAccepts();
      if (tries >= 30) clearInterval(timer);
    }, 3000);
  } catch (e) {}
})();
"""

# Consent wall ("Accept all" / "Принять все" / "Aceptar todo" ...).
#
# A fresh browser profile in the EU (Spain in the reports) is served a consent
# wall BEFORE the chat. The old build had no clicker at all and, worse, its
# target picker matched `consent.google.com` as "a google page" — automation
# attached to the wall and waited for a chat that never arrived. This runs on
# every document of the real browser, in every language Google ships, and never
# clicks a reject/manage control.
CONSENT_CLICK_JS = r"""
(() => {
  try {
    const norm = (v) => String(v || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const STRONG = /(accept all|allow all|принять все|принять всё|aceptar todo|consentir todo|alle akzeptieren|zustimmen alle|tout accepter|autoriser tout|accetta tutto|consenti tutto|aceitar tudo|permitir tudo|tümünü kabul|zaakceptuj wszystko|прийняти всі|alles accepteren|acceptera alla|godkänn alla|accepter alle|godta alle|hyväksy kaikki|přijmout vše|souhlasím se vším|elfogad mindent|αποδοχή όλων|すべて同意|すべて受け入れる|全部接受|接受全部|모두 동의|전체 동의|accept toate|prijať všetko|prihvati sve)/i;
    const WEAK = /^(accept|agree|i agree|allow|ok|okay|got it|принять|согласен|согласиться|подтвердить|разрешить|aceptar|acepto|de acuerdo|akzeptieren|zustimmen|einverstanden|accepter|j.accepte|d.accord|accetta|accetto|sono d.accordo|aceitar|aceito|concordo|kabul et|onayla|kabul ediyorum|zaakceptuj|akceptuj|akceptuję|zgadzam się|прийняти|згоден|accepteren|akkoord|godkänn|godta|hyväksy|souhlasím|elfogadom|αποδέχομαι|同意する|同意|接受|동의|accept toate|sunt de acord|prijať|prihvati)$/i;
    const REJECT = /(reject|decline|deny|manage|customi[sz]e|more option|learn more|отклон|отказ|настро|управл|подробн|друг| Ablehnen|ablehnen|rechazar|rehusar|refuser|rifiuta|recusar|reddet|odrzu[ćc]|elutas|ハイライト|拒否|거부|設定)/i;
    const accept = () => {
      let clicked = 0;
      let nodes = [];
      try { nodes = Array.prototype.slice.call(document.querySelectorAll('button, input[type="submit"], div[role="button"], a[role="button"]')); } catch (e) { return 0; }
      for (const el of nodes) {
        const label = norm(el.getAttribute && (el.getAttribute('aria-label') || el.innerText || el.value || el.textContent));
        if (!label || label.length > 64) continue;
        if (REJECT.test(label)) continue;
        if (!STRONG.test(label) && !WEAK.test(label)) continue;
        try { el.click(); clicked += 1; } catch (e) {}
      }
      if (!clicked) {
        // Google's own consent controls (ids/jsname are stable across locales).
        const selectors = ['button#L2AGLb', 'button[jsname="tWT92d"]',
                           'form[action*="consent"] button', 'div[role="dialog"] form button'];
        for (const sel of selectors) {
          try {
            const el = document.querySelector(sel);
            if (el) { el.click(); clicked += 1; break; }
          } catch (e) {}
        }
      }
      if (clicked) {
        try { window.__legalyzeConsentClicks = (window.__legalyzeConsentClicks || 0) + clicked; } catch (e) {}
      }
      return clicked;
    };
    window.__legalyzeAcceptConsent = accept;
    accept();
    let tries = 0;
    const timer = setInterval(() => {
      tries += 1;
      accept();
      if (tries >= 40) clearInterval(timer);
    }, 1000);
  } catch (e) {}
})();
"""

# ---------------------------------------------------------------------------
# Minimal QWebChannel client (fallback when the Qt resource copy of
# qwebchannel.js is unavailable). Mirrors the message protocol of the official
# Qt client: init/handshake (type 3/10), invokeMethod (6), connectToSignal (7),
# signal delivery (1). Covered by tests against a protocol-accurate mock.
QWEBCHANNEL_LITE_JS = r"""
window.QWebChannel = window.QWebChannel || function(transport, initCallback) {
  if (!transport || typeof transport.send !== 'function') {
    console.error('QWebChannel: invalid transport');
    return;
  }
  const T = {signal: 1, propertyUpdate: 2, init: 3, idle: 4, invokeMethod: 6,
             connectToSignal: 7, disconnectFromSignal: 8, response: 10};
  const channel = this;
  this.objects = {};
  let execId = 0;
  const execCallbacks = {};
  const send = (data) => { try { transport.send(JSON.stringify(data)); } catch (e) {} };
  const exec = (data, callback) => {
    if (callback) { data.id = execId++; execCallbacks[data.id] = callback; }
    send(data);
  };
  transport.onmessage = function(message) {
    let data = message && message.data;
    if (typeof data === 'string') { try { data = JSON.parse(data); } catch (e) { return; } }
    if (!data) return;
    if (data.type === T.response) {
      const cb = execCallbacks[data.id];
      delete execCallbacks[data.id];
      if (cb) { try { cb(data.data); } catch (e) {} }
    } else if (data.type === T.signal) {
      const obj = channel.objects[data.object];
      if (obj && obj.__emit) obj.__emit(data.signal, data.args);
    } else if (data.type === T.propertyUpdate) {
      exec({type: T.idle});
    }
  };

  function wrapObject(name, meta) {
    const obj = {__id__: name};
    channel.objects[name] = obj;
    const byIndex = {};
    (meta.signals || []).forEach((sig) => {
      const sigName = sig[0], sigIndex = sig[1];
      const handlers = [];
      byIndex[sigIndex] = handlers;
      obj[sigName] = {
        connect(cb) {
          if (typeof cb !== 'function') return;
          handlers.push(cb);
          if (handlers.length === 1) exec({type: T.connectToSignal, object: name, signal: sigIndex});
        },
        disconnect(cb) {
          const i = handlers.indexOf(cb);
          if (i >= 0) handlers.splice(i, 1);
          if (!handlers.length) exec({type: T.disconnectFromSignal, object: name, signal: sigIndex});
        }
      };
    });
    obj.__emit = (key, args) => {
      const handlers = byIndex[key] || [];
      handlers.forEach((cb) => { try { cb.apply(null, args || []); } catch (e) {} });
    };
    (meta.methods || []).forEach((m) => {
      const methodName = String(m[0]), methodIdx = m[1];
      const invoke = methodName.charAt(methodName.length - 1) === ')' ? methodIdx : methodName;
      obj[methodName.split('(')[0]] = function() {
        const args = [];
        let callback = null;
        for (let i = 0; i < arguments.length; i++) {
          if (typeof arguments[i] === 'function') callback = arguments[i];
          else args.push(arguments[i]);
        }
        exec({type: T.invokeMethod, object: name, method: invoke, args: args},
             callback ? (response) => callback(response) : null);
      };
    });
    return obj;
  }

  exec({type: T.init}, (data) => {
    Object.keys(data || {}).forEach((name) => wrapObject(name, data[name] || {}));
    if (typeof initCallback === 'function') { try { initCallback(channel); } catch (e) {} }
    exec({type: T.idle});
  });
};
"""

# ---------------------------------------------------------------------------
# Appended after the qwebchannel.js library (Qt resource or the lite client).
# Retries: `qt` and `QWebChannel` may appear slightly after document creation.
CHANNEL_INIT_JS = r"""
(() => {
  const connect = (tries) => {
    try {
      if (typeof qt === 'undefined' || !qt.webChannelTransport ||
          typeof QWebChannel === 'undefined') {
        if (tries > 0) setTimeout(() => connect(tries - 1), 50);
        return;
      }
      new QWebChannel(qt.webChannelTransport, function(channel) {
        try {
          window.__legalyzeSpeechBridge = channel.objects.legalyzeSpeech;
          const waiters = window.__legalyzeSpeechWaiters || [];
          window.__legalyzeSpeechWaiters = [];
          for (let i = 0; i < waiters.length; i++) {
            try { waiters[i](window.__legalyzeSpeechBridge); } catch (e) {}
          }
          try { window.__legalyzeSpeechBridge.reportCapabilities(JSON.stringify(window.__legalyzeSpeech || {})); } catch (e) {}
        } catch (e) {}
      });
    } catch (e) {
      if (tries > 0) setTimeout(() => connect(tries - 1), 50);
    }
  };
  connect(40);
})();
"""

# ---------------------------------------------------------------------------
# v18: ЯЗЫК РЕЧЕВОГО ВВОДА. Приложение русское, поэтому и распознавание
# обязано быть русским — независимо от языка Windows и страны пользователя.
#
# В нативном режиме (настоящий Chrome) объект SpeechRecognition НЕ подменяется:
# он остаётся родным, иначе речевой сервис Google перестал бы работать.
# Оборачивается только конструктор, чтобы свойство `lang` всегда было равным
# настроенному языку: страница ставит своё значение (у пользователя из США —
# en-US), а мы его принудительно держим на ru-RU.
JS_SPEECH_LANG = r"""
(() => {
    const LANG = '%s';
    window.__legalyzeSpeechLang = LANG;
    try { document.documentElement.setAttribute('lang', LANG); } catch (e) {}

    const Native = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Native) { return 'no-native'; }
    if (Native.__legalyzeLang) { return 'already'; }

    try {
        // Настоящий объект НЕ подменяется: подменяется только конструктор,
        // чтобы `lang` всегда уходил в движок распознавания приложения.
        const desc = Object.getOwnPropertyDescriptor(Native.prototype, 'lang');
        const Patched = function () {
            const rec = new Native();
            // Сначала — прямо в движок: распознавание реально пойдёт на LANG.
            try { if (desc && desc.set) desc.set.call(rec, LANG); } catch (e) {}
            try { rec.lang = LANG; } catch (e) {}
            // Затем — и читаемому значению, и любой попытке страницы его
            // сменить: приоритет остаётся у языка приложения.
            try {
                Object.defineProperty(rec, 'lang', {
                    configurable: true,
                    get() { try { return desc && desc.get ? desc.get.call(rec) : LANG; }
                            catch (e) { return LANG; } },
                    set(_value) {
                        try { if (desc && desc.set) desc.set.call(rec, LANG); } catch (e) {}
                    },
                });
            } catch (e) {}
            return rec;
        };
        Patched.prototype = Native.prototype;
        Patched.__legalyzeLang = true;
        window.SpeechRecognition = Patched;
        window.webkitSpeechRecognition = Patched;
        return 'patched';
    } catch (e) {
        return 'error';
    }
})();
"""


def speech_lang_js(lang='ru-RU'):
    """Скрипт с ПОДСТАВЛЕННЫМ языком (v18)."""
    return JS_SPEECH_LANG % str(lang or 'ru-RU')


# ---------------------------------------------------------------------------
# v18: СВЕТЛАЯ ТЕМА. На Windows с тёмной темой Chrome по умолчанию рисует
# страницу тёмной: `prefers-color-scheme: dark` + «Auto Dark Mode». Результат —
# у одного пользователя белый чат, у другого чёрный. Здесь тема фиксируется
# светлой: и сам Media-признак, и стиль корня документа.
JS_LIGHT_THEME = r"""
(() => {
    let changed = 0;
    try {
        const root = document.documentElement;
        if (root) {
            root.style.setProperty('color-scheme', 'light', 'important');
            root.setAttribute('data-legalyze-theme', 'light');
            changed++;
        }
    } catch (e) {}
    try {
        let meta = document.querySelector('meta[name="color-scheme"]');
        if (!meta) {
            meta = document.createElement('meta');
            meta.setAttribute('name', 'color-scheme');
            document.head.appendChild(meta);
        }
        meta.setAttribute('content', 'light');
        changed++;
    } catch (e) {}
    try {
        const style = document.createElement('style');
        style.textContent = ':root, html, body { color-scheme: light !important; }';
        document.head.appendChild(style);
        changed++;
    } catch (e) {}
    return changed;
})();
"""


# ---------------------------------------------------------------------------
# Purges unneeded site chrome (Google header, account menu, navigation,
# snackbars, the G-logo). Runs at document creation to avoid any visual flash
# and is re-applied over CDP after attach. Protected: the whole input area,
# microphone/send/file controls and their icons.
JS_PURGE = r"""
(() => {
    if (window.__purgeInstalled) {
        if (window.__purgeRun) window.__purgeRun();
        return;
    }

    window.__purgeInstalled = true;

    const SELECTORS = [
        'div.qEn1od[jsname="NlVIob"]',
        'div.qEn1od',
        'div.P3mIxe.Hw60ud',
        'div.FSUH7d[jsname="xcvsnc"]',
        'div.GG4mbd[role="navigation"]',
        'div.eT9Cje',
        'span.gb',
        'div[jscontroller="SJpD2c"][jsname="uZkjhb"]',
        'header#gb',
        'div#gbwa',
        'g-snackbar[jsname="PWj1Zb"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"]',
        'svg[width="26"][height="26"][viewBox="0 0 24 24"] *'
    ];

    const S = SELECTORS.join(',');

    // Никогда не трогаем панель ввода, микрофон, отправку и файлы.
    const INPUT_AREA = 'form, footer, .esoFne, .Txyg0d, .CEpIFc, [role="region"], [data-xid*="input-plate"]';
    const PROTECTED_LABEL = /икрофон|микрофон|mic|voice|голос|отправ|send|dictat|дикт|вопрос|attach|прикреп|файл|file/i;
    const isProtected = (el) => {
        try {
            if (!el || el.nodeType !== 1) return true;
            if (el.closest && el.closest(INPUT_AREA)) return true;
            const label = ((el.getAttribute && (el.getAttribute('aria-label') || el.getAttribute('data-xid') || el.getAttribute('title'))) || '');
            if (PROTECTED_LABEL.test(label)) return true;
            const text = (el.textContent || '').slice(0, 120);
            if (PROTECTED_LABEL.test(text)) return true;
            return false;
        } catch (e) { return true; }
    };

    const hide = (el) => {
        if (!el || isProtected(el)) return;
        if (el.style) {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
        }
        try { el.remove(); } catch (e) {}
    };

    const kill = (node) => {
        if (!node || node.nodeType !== 1) return;

        if (node.matches && node.matches(S)) {
            hide(node);
            return;
        }

        if (node.querySelectorAll) {
            const list = node.querySelectorAll(S);
            for (let i = list.length - 1; i >= 0; i--) hide(list[i]);
        }
    };

    window.__purgeRun = () => kill(document.documentElement);

    const obs = new MutationObserver((mutations) => {
        for (const m of mutations) {
            if (m.type === 'childList') {
                for (const n of m.addedNodes) kill(n);
            } else if (m.type === 'attributes') {
                kill(m.target);
            }
        }
    });

    const start = () => {
        const root = document.documentElement;
        if (!root) return false;

        obs.observe(root, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ['class', 'id', 'jsname', 'jscontroller']
        });

        kill(root);
        return true;
    };

    if (!start()) {
        const t = setInterval(() => {
            if (start()) clearInterval(t);
        }, 10);
    }

    document.addEventListener('DOMContentLoaded', () => kill(document.documentElement), { once: true });
    window.addEventListener('load', () => kill(document.documentElement), { once: true });
})();
"""

# ---------------------------------------------------------------------------
CAPABILITY_JS = r"""(() => {
  const d = navigator.userAgentData;
  return {
    secure: window.isSecureContext === true,
    mediaDevices: !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia),
    mediaRecorder: typeof MediaRecorder !== 'undefined',
    speechNative: !!window.__legalyzeNativeSpeechRecognition,
    speechShim: window.__legalyzeSpeechShimInstalled === true,
    uaPatched: window.__legalyzeUaPatched === true,
    brands: (d && d.brands) ? d.brands.map((b) => b.brand + '/' + b.version).join(',') : ''
  };
})()"""

# ---------------------------------------------------------------------------
# Рабочая кнопка микрофона: если сайт не отрисовал свою кнопку (или скрыл её),
# в панель ввода вставляется реплика штатной кнопки (разметка — как у кнопки
# отправки сайта: data-xid/aria-label/Material-иконка микрофона). Кнопка
# запускает НАСТОЯЩУЮ диктовку через наш SpeechRecognition-мост (Windows
# System.Speech), текст появляется в поле ввода; повторное нажатие останавливает
# запись. Если штатная кнопка есть — реплика не вставляется, работает штатная.
MIC_BUTTON_JS = r"""(() => {
  if (window.__legalyzeMicInstalled) {
    try { window.__legalyzeMic && window.__legalyzeMic.ensure(); } catch (e) {}
    return;
  }
  window.__legalyzeMicInstalled = true;

  const LOG = (m) => { try { console.debug('[legalyze-mic]', m); } catch (e) {} };
  const SELECTORS = {
    mic: 'button[data-xid="input-plate-voice-button"], button[data-xid="h4gG8"], button.uMMzHc.vpw7Fc, button.vpw7Fc, div[role="button"][aria-label="Микрофон"], button[aria-label="Микрофон"], button[aria-label="Использовать микрофон"], button[aria-label*="Микрофон"], button[aria-label*="икрофон"], button[aria-label*="Microphone"], button[aria-label*="icrophone"], button[aria-label*="дикт"], button[aria-label*="Дикт"], button[aria-label*="dictat"], button[aria-label*="Dictat"], button[title*="Микрофон"], button[title*="icrophone"], button[title*="дикт"]',
    send: 'button[data-xid="input-plate-voice-send-button"], button[data-xid="input-plate-send-button"], button[aria-label="Отправить"], button[aria-label="Send"], button[title="Отправить"], button.send-button',
    editor: 'textarea.ITIRGe, textarea[aria-label="Задайте вопрос"], textarea[placeholder*="Спросите"], textarea[placeholder*="Ask"], textarea, div[contenteditable="true"]'
  };
  const visible = (el) => {
    try {
      return !!(el.getClientRects().length || el.offsetWidth || el.offsetHeight);
    } catch (e) { return true; }
  };
  const findNativeMic = () => {
    try {
      const list = document.querySelectorAll(SELECTORS.mic);
      for (const el of list) {
        if (el.id === 'legalyze-mic') continue;
        try {
          if (el.closest && el.closest('div[aria-hidden="true"]')) continue;
        } catch (e) {}
        if (visible(el)) return el;
      }
    } catch (e) {}
    return null;
  };
  const findEditor = () => {
    try {
      const list = document.querySelectorAll(SELECTORS.editor);
      for (const el of list) {
        if (!el.isConnected) continue;
        if (visible(el)) return el;
      }
    } catch (e) {}
    return null;
  };
  const writeEditor = (text) => {
    const ed = findEditor();
    if (!ed) return false;
    try {
      const tag = (ed.tagName || '').toLowerCase();
      if (tag === 'textarea' || tag === 'input') {
        try {
          if (ed.style) { ed.style.display = ''; ed.style.visibility = ''; }
        } catch (e) {}
        let setter = null;
        try {
          const proto = tag === 'textarea' && typeof HTMLTextAreaElement !== 'undefined'
            ? HTMLTextAreaElement.prototype
            : (typeof HTMLInputElement !== 'undefined' ? HTMLInputElement.prototype : null);
          const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) setter = desc.set;
        } catch (e) {}
        if (setter) setter.call(ed, text); else ed.value = text;
        ed.dispatchEvent(new Event('input', { bubbles: true }));
        ed.dispatchEvent(new Event('change', { bubbles: true }));
      } else {
        ed.textContent = text;
        ed.dispatchEvent(new Event('input', { bubbles: true }));
      }
      return true;
    } catch (e) { LOG('write fail: ' + e); return false; }
  };
  const setRecordingUI = (on) => {
    try {
      const btn = document.getElementById('legalyze-mic');
      if (btn) {
        btn.classList.toggle('rec', !!on);
        btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      }
      let hint = document.getElementById('legalyze-mic-hint');
      if (on) {
        if (!hint) {
          hint = document.createElement('div');
          hint.id = 'legalyze-mic-hint';
          hint.textContent = 'Преобразование речи в текст…';
          try {
            hint.style.cssText = 'position:absolute;bottom:100%;left:12px;margin-bottom:6px;font-size:12px;opacity:.72;pointer-events:none;';
          } catch (e) {}
          const btn2 = document.getElementById('legalyze-mic');
          if (btn2 && btn2.parentElement) btn2.parentElement.appendChild(hint);
        }
        hint.style.display = '';
      } else if (hint) {
        hint.style.display = 'none';
      }
    } catch (e) {}
  };

  const state = { rec: null, active: false, finals: [], interim: '' };
  const api = {
    active: () => state.active === true,
    ensure: () => ensureButton(),
    // Явный старт: повторный вызов перезапускает запись, а не «отправляет»
    // (состояние страницы могло рассинхронизироваться, если движок диктовки
    // не прислал событий) — см. v6, реальный лог 20260930.
    start: () => {
      try { if (state.rec) state.rec.stop(); } catch (e) {}
      state.active = false; state.rec = null;
      return api._startDictation();
    },
    toggle: () => {
      if (state.active) { api.stop(); return false; }
      return api._startDictation();
    },
    _startDictation: () => {
      let SR = null;
      try { SR = window.LegalyzeSpeechRecognition || window.SpeechRecognition || window.webkitSpeechRecognition; } catch (e) {}
      if (!SR) { LOG('SpeechRecognition недоступен'); return false; }
      try {
        const rec = new SR();
        // v18: язык задан приложением и имеет ПРИОРИТЕТ над языком системы.
        // Раньше здесь первым был navigator.language, из-за чего у
        // пользователя из США распознавание шло на английском.
        rec.lang = window.__legalyzeSpeechLang || navigator.language || 'ru-RU';
        rec.continuous = true;
        rec.interimResults = true;
        rec.maxAlternatives = 1;
        state.finals = []; state.interim = ''; state.rec = rec; state.active = true;
        rec.onstart = () => setRecordingUI(true);
        rec.onresult = (ev) => {
          try {
            let interim = '';
            const start = ev.resultIndex || 0;
            for (let i = start; i < ev.results.length; i++) {
              const r = ev.results[i];
              const t = (r && r[0] && r[0].transcript) || '';
              if (r.isFinal) state.finals.push(t); else interim += t;
            }
            state.interim = interim;
            const text = (state.finals.join(' ') + ' ' + state.interim).replace(/\s+/g, ' ').trim();
            writeEditor(text);
          } catch (e) { LOG('result fail: ' + e); }
        };
        rec.onerror = (ev) => LOG('rec error: ' + (ev && ev.error));
        rec.onend = () => {
          if (state.rec === rec) {
            state.active = false; state.rec = null;
            setRecordingUI(false);
          }
        };
        rec.start();
        LOG('dictation started');
        return true;
      } catch (e) {
        LOG('start fail: ' + e);
        state.active = false; state.rec = null;
        setRecordingUI(false);
        return false;
      }
    },
    stop: () => {
      try { if (state.rec) state.rec.abort(); } catch (e) {}
      try { if (state.rec) state.rec.stop(); } catch (e) {}
      state.active = false; state.rec = null; state.interim = '';
      setRecordingUI(false);
      return true;
    }
  };
  window.__legalyzeDictate = api;
  window.__legalyzeMic = api;

  const closestSend = (node) => {
    let el = node;
    for (let i = 0; el && i < 8; i++) {
      try { if (el.matches && el.matches(SELECTORS.send)) return el; } catch (e) {}
      el = el.parentElement || el.parentNode;
    }
    return null;
  };
  const closestMic = (node) => {
    let el = node;
    for (let i = 0; el && i < 10; i++) {
      try {
        if (el.matches && el.matches(SELECTORS.send)) return null;
        if (el.matches && el.matches(SELECTORS.mic)) return el;
        if (el.tagName === 'path') {
          const d = (el.getAttribute && el.getAttribute('d')) || '';
          if (d.indexOf('M480-400') >= 0 || d.indexOf('M12 14c1.66') >= 0 ||
              d.indexOf('M12 2a3 3 0') >= 0) {
            const btn = el.closest && el.closest('button, div[role="button"]');
            if (btn && !(btn.getAttribute('data-xid') || '').includes('send')) return btn;
          }
        }
      } catch (e) {}
      el = el.parentElement || el.parentNode;
    }
    return null;
  };

  // Жёсткий перехват ЛЮБОЙ кнопки микрофона (штатной или реплики).
  // Клик по штатной кнопке запускает веб-конвейер сайта (getUserMedia),
  // который в QtWebEngine без WebRTC РОНЯЕТ рендер-процесс (страница
  // пустеет, loadFinished(False) -> "Страница не загружена"). Поэтому все
  // pointer/click события на кнопке микрофона перехватываются ДО обработчиков
  // сайта и уводятся в НАСТОЯЩУЮ диктовку (Windows System.Speech).
  let lastToggleTs = 0;
  const hijack = (ev) => {
    try {
      const t = closestMic(ev.target);
      if (!t) return;
      try {
        ev.preventDefault();
        ev.stopPropagation();
        if (ev.stopImmediatePropagation) ev.stopImmediatePropagation();
      } catch (e) {}
      if (ev.type !== 'click') return;
      // Один тумблер на серию кликов (synthetic + el.click() + CDP-клик).
      const now = Date.now();
      if (now - lastToggleTs < 500) return;
      lastToggleTs = now;
      api.toggle();
    } catch (e) {}
  };
  for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup',
                   'touchstart', 'touchend', 'click']) {
    document.addEventListener(t, hijack, true);
  }
  document.addEventListener('click', (ev) => {
    try {
      if (!state.active) return;
      const t = closestSend(ev.target);
      if (t && t.id !== 'legalyze-mic') api.stop();
    } catch (e) {}
  }, true);
  document.addEventListener('keydown', (ev) => {
    try {
      if (state.active && ev.key === 'Enter' && !ev.shiftKey && ev.target === findEditor()) api.stop();
    } catch (e) {}
  }, true);

  let ensureTimer = null;
  const ensureButton = () => {
    try {
      const native = findNativeMic();
      const ours = document.getElementById('legalyze-mic');
      if (native) {
        if (ours && ours.parentElement) ours.parentElement.removeChild(ours);
        return;
      }
      if (ours) return;
      const editor = findEditor();
      if (!editor) return;
      let anchor = null;
      try { anchor = document.querySelector(SELECTORS.send); } catch (e) {}
      let host = null;
      if (anchor && anchor.parentElement) host = anchor.parentElement;
      else {
        try {
          const plate = document.querySelector('[data-xid*="input-plate"]');
          host = (plate && plate.querySelector('div')) || (editor && editor.parentElement) || plate;
        } catch (e) {
          host = editor && editor.parentElement;
        }
      }
      if (!host) return;

      const btn = document.createElement('button');
      btn.id = 'legalyze-mic';
      btn.type = 'button';
      btn.className = 'uMMzHc Z3UdVc IbS5tc legalyze-mic-btn';
      btn.setAttribute('data-xid', 'input-plate-voice-button');
      btn.setAttribute('aria-label', 'Микрофон');
      btn.setAttribute('title', 'Микрофон');
      btn.setAttribute('jsname', 'Vnjt8d');
      btn.setAttribute('aria-pressed', 'false');
      try {
        btn.style.cssText = 'width:40px;height:40px;border-radius:50%;border:0;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;background:transparent;color:inherit;margin-inline-start:8px;position:relative;';
      } catch (e) {}
      btn.innerHTML = '<div class="DJ8aee" aria-hidden="true"></div><div class="wilSz Iq9dx" aria-hidden="true"><svg aria-hidden="true" fill="currentColor" height="24px" viewBox="0 -960 960 960" width="24px" style="pointer-events:none"><path d="M480-400q-50 0-85-35t-35-85v-240q0-50 35-85t85-35q50 0 85 35t35 85v240q0 50-35 85t-85 35ZM160-160v-100q0-34 17.5-62.5T224-378q62-31 126-46.5T480-440q66 0 130 15.5T736-378q29 15 46.5 43.5T800-260v100H160Z"></path></svg></div>';
      const style = document.createElement('style');
      style.textContent = '#legalyze-mic.rec{color:#d93025;animation:legalyzeMicPulse 1.1s infinite}@keyframes legalyzeMicPulse{0%,100%{transform:scale(1)}50%{transform:scale(1.12)}}#legalyze-mic:hover{background:rgba(128,128,128,.14)}';
      if (document.head) document.head.appendChild(style);
      // Клик по реплике обрабатывает общий перехват (hijack) выше — один
      // тумблер на серию кликов, обработчики сайта не получают ничего.
      if (anchor && anchor.parentElement === host && host.insertBefore) host.insertBefore(btn, anchor);
      else host.appendChild(btn);
      LOG('replica injected');
    } catch (e) { LOG('ensure fail: ' + e); }
  };

  try {
    if (typeof MutationObserver !== 'undefined') {
      let scheduled = false;
      const mo = new MutationObserver(() => {
        if (scheduled) return;
        scheduled = true;
        setTimeout(() => { scheduled = false; ensureButton(); }, 300);
      });
      mo.observe(document.documentElement, { childList: true, subtree: true });
    }
    ensureTimer = setInterval(ensureButton, 2000);
    if (ensureTimer && ensureTimer.unref) ensureTimer.unref();
  } catch (e) {}
  try {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => ensureButton(), { once: true });
    } else {
      ensureButton();
    }
  } catch (e) { ensureButton(); }
})();
"""

# Одношаговые действия для main.py: вкл/выкл собственной диктовки.
JS_MIC_TOGGLE = "!!(window.__legalyzeMic && window.__legalyzeMic.toggle())"
JS_MIC_START = ("(function(){try{var m=window.__legalyzeMic;if(!m)return false;"
               "if(m.active()){try{m.stop()}catch(e){}}"
               "return !!m.start()}catch(e){return false}})()")
JS_MIC_STOP = "(function(){try{window.__legalyzeMic && window.__legalyzeMic.stop()}catch(e){};return true})()"
