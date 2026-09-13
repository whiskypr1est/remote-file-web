/* ==========================================================================
   登录页逻辑
   --------------------------------------------------------------------------
   流程与 Windows 一致：
     1. 先显示锁屏（大时钟 +「点击任意位置继续」）
     2. 点击/按键后滑出登录卡片
     3. 提交后校验，成功跳转到桌面，失败就地给出错误提示
   如果检测到会话仍然有效，则直接进入桌面，不用重复登录。
   ========================================================================== */
(function () {
  "use strict";

  var el = {
    clockTime: document.getElementById("clockTime"),
    clockDate: document.getElementById("clockDate"),
    hint: document.getElementById("lockHint"),
    panel: document.getElementById("loginPanel"),
    form: document.getElementById("loginForm"),
    username: document.getElementById("username"),
    password: document.getElementById("password"),
    submit: document.getElementById("loginBtn"),
    error: document.getElementById("loginError"),
    host: document.getElementById("serverHost"),
    boot: document.getElementById("bootMask")
  };

  var submitting = false;

  /* ---------------- 时钟 ---------------- */

  var WEEKDAYS = ["星期日", "星期一", "星期二", "星期三", "星期四", "星期五", "星期六"];

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function tick() {
    var now = new Date();
    if (el.clockTime) {
      el.clockTime.textContent = pad(now.getHours()) + ":" + pad(now.getMinutes());
    }
    if (el.clockDate) {
      el.clockDate.textContent =
        now.getFullYear() + "年" + (now.getMonth() + 1) + "月" + now.getDate() + "日 " +
        WEEKDAYS[now.getDay()];
    }
  }

  tick();
  setInterval(tick, 1000);

  /* ---------------- 锁屏 -> 登录卡片 ---------------- */

  function showPanel() {
    if (!el.panel || el.panel.classList.contains("show")) {
      return;
    }
    el.panel.hidden = false;
    if (el.hint) {
      el.hint.classList.add("hidden");
    }
    // 触发过渡动画
    requestAnimationFrame(function () {
      el.panel.classList.add("show");
    });
    setTimeout(function () {
      if (el.password) {
        el.password.focus();
      }
    }, 260);
  }

  if (el.hint) {
    el.hint.addEventListener("click", showPanel);
  }
  document.addEventListener("keydown", function (e) {
    if (!el.panel || !el.panel.classList.contains("show")) {
      // 任意按键都可以进入登录界面（除修饰键）
      if (["Shift", "Control", "Alt", "Meta", "CapsLock", "Tab"].indexOf(e.key) === -1) {
        e.preventDefault();
        showPanel();
      }
    }
  });
  document.addEventListener("click", function (e) {
    if (el.panel && !el.panel.classList.contains("show")) {
      showPanel();
    }
  });

  // 地址栏加 #login 可直接进入密码输入框（方便把这个页面加入收藏夹）
  if (location.hash === "#login") {
    showPanel();
  }

  /* ---------------- 错误提示 ---------------- */

  function showError(message) {
    if (!el.error) {
      return;
    }
    el.error.textContent = message || "";
    el.error.classList.remove("show");
    // 强制重排以便重新播放抖动动画
    void el.error.offsetWidth;
    el.error.classList.add("show");
  }

  function setBusy(busy) {
    submitting = busy;
    if (el.submit) {
      el.submit.disabled = busy;
      el.submit.textContent = busy ? "…" : "→";
    }
    if (el.password) {
      el.password.disabled = busy;
    }
    if (el.username) {
      el.username.disabled = busy;
    }
  }

  /* ---------------- 登录 ---------------- */

  function login() {
    if (submitting) {
      return;
    }
    var username = (el.username && el.username.value || "").trim();
    var password = (el.password && el.password.value) || "";

    if (!username) {
      showError("请输入用户名");
      el.username && el.username.focus();
      return;
    }
    if (!password) {
      showError("请输入密码");
      el.password && el.password.focus();
      return;
    }

    setBusy(true);
    showError("");

    fetch("/api/auth/login", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: password })
    }).then(function (res) {
      return res.json().catch(function () {
        return {};
      }).then(function (data) {
        return { ok: res.ok, status: res.status, data: data };
      });
    }).then(function (result) {
      if (result.ok && result.data && result.data.ok) {
        // 登录成功，进入桌面
        location.replace("/");
        return;
      }
      setBusy(false);
      var msg = (result.data && (result.data.message || result.data.detail)) || "";
      if (!msg) {
        msg = result.status === 429
          ? "登录失败次数过多，请稍后再试"
          : "登录失败，请检查用户名和密码";
      }
      showError(msg);
      if (el.password) {
        el.password.value = "";
        el.password.focus();
      }
    }).catch(function (err) {
      setBusy(false);
      showError("无法连接到服务器：\n" + (err && err.message ? err.message : err));
    });
  }

  if (el.form) {
    el.form.addEventListener("submit", function (e) {
      e.preventDefault();
      login();
    });
  }

  /* ---------------- 初始化 ---------------- */

  if (el.host) {
    el.host.textContent = "服务器 " + location.host;
  }

  // 会话仍然有效则直接进入桌面
  fetch("/api/auth/status", { credentials: "same-origin" })
    .then(function (res) {
      return res.json();
    })
    .then(function (data) {
      if (data && data.authenticated) {
        location.replace("/");
      }
    })
    .catch(function () {
      /* 忽略：网络异常时仍允许手动登录 */
    })
    .then(function () {
      if (el.boot) {
        el.boot.classList.add("hidden");
      }
      setTimeout(function () {
        if (el.password) {
          el.password.focus();
        }
      }, 120);
    });
})();
