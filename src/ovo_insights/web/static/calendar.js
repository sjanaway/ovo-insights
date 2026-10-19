// Shows the details of the calendar square under the mouse (or tapped, or focused) in the line
// below its calendar. The squares also carry a `title`, but browsers show that slowly, not at
// all in some views, and never on a touch screen.
(function () {
  "use strict";

  document.querySelectorAll(".calendar[data-detail-id]").forEach(function (calendar) {
    var out = document.getElementById(calendar.getAttribute("data-detail-id"));
    if (!out) { return; }
    function show(event) {
      var cell = event.target;
      if (cell && cell.classList && cell.classList.contains("cell") && cell.hasAttribute("data-detail")) {
        out.textContent = cell.getAttribute("data-detail");
      }
    }
    calendar.addEventListener("mouseover", show);
    calendar.addEventListener("click", show);
    calendar.addEventListener("focusin", show);
  });
})();
