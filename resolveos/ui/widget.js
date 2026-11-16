/* ResolveOS chat widget. Embed:
   <script src="https://YOUR-HOST/widget.js" data-token="wt_..."></script> */
(function () {
  "use strict";
  var me = document.currentScript, TOKEN = me.getAttribute("data-token");
  var BASE = me.src.replace(/widget\.js(\?.*)?$/, "");
  var css = ".ros-b{position:fixed;right:22px;bottom:22px;width:58px;height:58px;border-radius:50%;border:0;cursor:pointer;z-index:2147483000;" +
    "background:conic-gradient(from 0deg,#3ee6ff,#9b7bff,#ff6fd8,#3ee6ff);box-shadow:0 10px 30px rgba(62,230,255,.35);color:#05070d;font:700 22px system-ui}" +
    ".ros-p{position:fixed;right:22px;bottom:92px;width:340px;max-width:92vw;background:#0b1120;color:#dce6f7;border:1px solid #23314f;border-radius:16px;" +
    "box-shadow:0 20px 60px rgba(0,0,0,.5);font:14px system-ui;z-index:2147483000;display:none;overflow:hidden}" +
    ".ros-p header{padding:14px 16px;background:linear-gradient(90deg,rgba(62,230,255,.18),rgba(155,123,255,.12));font-weight:600}" +
    ".ros-p .ros-c{padding:14px 16px;max-height:320px;overflow:auto;display:flex;flex-direction:column;gap:8px}" +
    ".ros-m{padding:9px 12px;border-radius:12px;max-width:85%;white-space:pre-wrap;line-height:1.4}.ros-u{align-self:flex-end;background:#1d2a4a}.ros-a{background:#13213a;border:1px solid #23314f}" +
    ".ros-p input,.ros-p textarea{width:100%;box-sizing:border-box;background:#05070d;color:#dce6f7;border:1px solid #23314f;border-radius:10px;padding:9px;margin-top:8px;font:inherit}" +
    ".ros-p button.s{width:100%;margin-top:8px;padding:10px;border:0;border-radius:10px;background:#3ee6ff;color:#04121a;font-weight:700;cursor:pointer}" +
    ".ros-f{padding:0 16px 14px}.ros-t{color:#7f8ca8;font-size:12px}";
  var st = document.createElement("style"); st.textContent = css; document.head.appendChild(st);
  var b = document.createElement("button"); b.className = "ros-b"; b.textContent = "?"; b.title = "Support";
  var p = document.createElement("div"); p.className = "ros-p";
  p.innerHTML = '<header>Support · usually resolved in seconds</header><div class="ros-c" id="ros-c"><div class="ros-m ros-a">Hi! Tell us what you need: refunds, order changes, deliveries, account help.</div></div>' +
    '<div class="ros-f"><input id="ros-e" type="email" placeholder="your email"><textarea id="ros-x" rows="3" placeholder="How can we help? Include your order number if you have it."></textarea><button class="s" id="ros-s">Send</button><div class="ros-t" id="ros-t"></div></div>';
  document.body.appendChild(b); document.body.appendChild(p);
  b.onclick = function () { p.style.display = p.style.display === "block" ? "none" : "block"; };
  var c = p.querySelector("#ros-c"), state = null, seen = 0;
  function add(t, who) { var d = document.createElement("div"); d.className = "ros-m " + (who === "u" ? "ros-u" : "ros-a"); d.textContent = t; c.appendChild(d); c.scrollTop = 1e9; }
  function poll() {
    if (!state) return;
    fetch(BASE + "v1/intake/chat/" + state.ticket_id + "?k=" + encodeURIComponent(state.key)).then(function (r) { return r.json(); }).then(function (d) {
      (d.replies || []).slice(seen).forEach(function (r) { add(r.final, "a"); }); seen = (d.replies || []).length;
      p.querySelector("#ros-t").textContent = d.status === "open" ? "Working on it…" : "Ticket " + d.status;
    }).catch(function () {});
  }
  setInterval(poll, 3000);
  p.querySelector("#ros-s").onclick = function () {
    var e = p.querySelector("#ros-e").value.trim(), x = p.querySelector("#ros-x").value.trim();
    if (!e || !x) return;
    add(x, "u"); p.querySelector("#ros-x").value = "";
    fetch(BASE + "v1/intake/chat", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({token: TOKEN, email: e, message: x})})
      .then(function (r) { return r.json(); }).then(function (d) { if (d.ticket_id) { state = d; seen = 0; p.querySelector("#ros-t").textContent = "Ticket " + d.ticket_id + " · working on it…"; } else add("Sorry, something went wrong.", "a"); });
  };
})();
