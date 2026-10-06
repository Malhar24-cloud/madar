// سكربت صغير فقط: أزرار الطباعة واقتراحات المساعد. كل شيء آخر يعمل بدون JavaScript.
document.addEventListener("click", function (ev) {
  var p = ev.target.closest("[data-print]");
  if (p) { window.print(); return; }
  var s = ev.target.closest("[data-suggest]");
  if (s) {
    var box = document.getElementById("q");
    if (box) { box.value = s.getAttribute("data-suggest"); box.form.requestSubmit ? box.form.requestSubmit() : box.form.submit(); }
  }
});
var end = document.getElementById("end");
if (end) { var m = document.querySelector(".msgs"); if (m) m.scrollTop = m.scrollHeight; }
