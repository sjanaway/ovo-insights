// Draws every element with data-chart="<kind>" from the JSON at its data-src.
//
// Same-origin GETs only. No eval, no inline HTML: tooltips use ECharts' canvas "richText"
// mode so they need no inline styles (blocked by the page's Content-Security-Policy).
// Money arrives as exact decimal strings and is only converted to a number for drawing.
(function () {
  "use strict";

  var css = getComputedStyle(document.documentElement);
  function token(name) { return css.getPropertyValue(name).trim(); }
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  var colours = {
    electricity: dark ? "#6aa8ff" : "#2563c9",
    gas: dark ? "#ff9f5a" : "#c25a0b",
    standing: dark ? "#8b8f96" : "#a9adb3",
    previous: dark ? "#8b8f96" : "#9aa0a6",
  };

  function money(text) { return text === null ? null : Number(text); }
  // Periods only partly inside the range are drawn faded, so a short week isn't misread.
  function faded(values, partial) {
    return values.map(function (v, i) { return partial[i] ? { value: v, itemStyle: { opacity: 0.4 } } : v; });
  }
  function partNote(d, i) { return d.partial[i] ? "\n(only partly in this range)" : ""; }
  function pounds(text) {
    return text === null ? "no cost data" :
      "£" + Number(text).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function kwh(value) {
    return value === null || value === undefined ? "no data" :
      value.toLocaleString("en-GB", { maximumFractionDigits: 3 }) + " kWh";
  }
  function dateLabel(iso, grain) {
    var d = new Date(iso + "T12:00:00Z");
    if (grain === "month") return d.toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });
    if (grain === "week") return "w/c " + d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit", timeZone: "UTC" });
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "2-digit", timeZone: "UTC" });
  }

  function base() {
    return {
      backgroundColor: "transparent",
      textStyle: { color: token("--fg") },
      grid: { left: 56, right: 16, top: 36, bottom: 40, containLabel: false },
      tooltip: { trigger: "axis", renderMode: "richText", confine: true },
      legend: { top: 0, textStyle: { color: token("--fg") } },
      animation: false,
    };
  }
  function axisStyle() {
    return {
      axisLine: { lineStyle: { color: token("--line") } },
      axisLabel: { color: token("--muted") },
      splitLine: { lineStyle: { color: token("--line") } },
    };
  }

  var builders = {
    usage: function (d) {
      var labels = d.labels.map(function (l) { return dateLabel(l, d.grain); });
      return Object.assign(base(), {
        title: { text: "kWh per " + d.grain, left: "right", textStyle: { fontSize: 12, color: token("--muted") } },
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return labels[i] + "\n" + kwh(d.kwh[i]) + " (" + d.days[i] + " days)" + partNote(d, i);
          },
        }),
        legend: { show: false },
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "kWh" }, axisStyle()),
        series: [{ type: "bar", data: faded(d.kwh, d.partial), itemStyle: { color: colours[d.fuel] } }],
      });
    },
    cost: function (d) {
      var labels = d.labels.map(function (l) { return dateLabel(l, d.grain); });
      return Object.assign(base(), {
        title: { text: "£ per " + d.grain, left: "right", textStyle: { fontSize: 12, color: token("--muted") } },
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return labels[i] + "\nUsage: " + pounds(d.usage_cost[i]) + "\nStanding charge: " + pounds(d.standing[i]) +
              (d.costed_days[i] < d.days[i] ? "\n(" + (d.days[i] - d.costed_days[i]) + " days without cost data)" : "") +
              partNote(d, i);
          },
        }),
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "£" }, axisStyle()),
        series: [
          { name: "Usage", type: "bar", stack: "cost", data: faded(d.usage_cost.map(money), d.partial), itemStyle: { color: colours[d.fuel] } },
          { name: "Standing charge", type: "bar", stack: "cost", data: faded(d.standing.map(money), d.partial), itemStyle: { color: colours.standing } },
        ],
      });
    },
    yoy: function (d) {
      var labels = d.labels.map(function (l) { return d.grain === "day" ? dateLabel(l, "day") : l; });
      return Object.assign(base(), {
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return labels[i] + "\nThis period: " + kwh(d.current[i]) + "\nA year earlier: " + kwh(d.previous[i]);
          },
        }),
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: d.grain === "week" ? "kWh per week (whole weeks)" : "kWh per day" }, axisStyle()),
        series: [
          { name: d.current_name, type: "line", data: d.current, connectNulls: false, showSymbol: false, itemStyle: { color: colours[d.fuel] } },
          { name: d.previous_name, type: "line", data: d.previous, connectNulls: false, showSymbol: false, lineStyle: { type: "dashed" }, itemStyle: { color: colours.previous } },
        ],
      });
    },
    heatmap: function (d) {
      var days = d.days.map(function (l) { return dateLabel(l, "day"); });
      return Object.assign(base(), {
        grid: { left: 56, right: 16, top: 16, bottom: 90 },
        tooltip: {
          renderMode: "richText", confine: true,
          formatter: function (p) {
            var v = p.data;
            return days[v[0]] + " " + d.slots[v[1]] + "\n" + kwh(v[2]) + (v[3] > 1 ? "\n(clocks went back: average of " + v[3] + ")" : "");
          },
        },
        legend: { show: false },
        xAxis: Object.assign({ type: "category", data: days, splitArea: { show: false } }, axisStyle()),
        yAxis: Object.assign({ type: "category", data: d.slots, inverse: true }, axisStyle()),
        visualMap: {
          // Cells are [day, slot, kWh, readings]: colour by kWh, not the last value.
          dimension: 2,
          min: 0, max: d.max || 1, precision: 2, calculable: true, orient: "horizontal", left: "center", bottom: 0,
          textStyle: { color: token("--muted") },
          inRange: { color: dark ? ["#1b2a3d", "#2b6cb0", "#9ad0ff"] : ["#eef4fb", "#5b9bd5", "#0b3d91"] },
        },
        series: [{ type: "heatmap", data: d.cells, progressive: 5000 }],
      });
    },
    profile: function (d) {
      return Object.assign(base(), {
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return d.slots[i] + "\nWeekdays: " + kwh(d.weekday_mean[i]) + " mean, " + kwh(d.weekday_median[i]) + " median" +
              "\nWeekends: " + kwh(d.weekend_mean[i]) + " mean, " + kwh(d.weekend_median[i]) + " median";
          },
        }),
        xAxis: Object.assign({ type: "category", data: d.slots }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "kWh per half-hour" }, axisStyle()),
        series: [
          { name: "Weekday mean", type: "line", data: d.weekday_mean, showSymbol: false, itemStyle: { color: colours.electricity } },
          { name: "Weekday median", type: "line", data: d.weekday_median, showSymbol: false, lineStyle: { type: "dashed" }, itemStyle: { color: colours.electricity } },
          { name: "Weekend mean", type: "line", data: d.weekend_mean, showSymbol: false, itemStyle: { color: colours.gas } },
          { name: "Weekend median", type: "line", data: d.weekend_median, showSymbol: false, lineStyle: { type: "dashed" }, itemStyle: { color: colours.gas } },
        ],
      });
    },
    baseload: function (d) {
      // Base load per night in watts; a night with a missing half-hour is a gap, not a guess.
      var labels = d.labels.map(function (l) { return dateLabel(l, "day"); });
      var name = d.wraps ? "Night from " + d.window.replace("\u2013", " to ") : d.window.replace("\u2013", " to ");
      return Object.assign(base(), {
        title: { text: "Lowest-10% half-hour, " + d.window + " (W)", left: "right", textStyle: { fontSize: 12, color: token("--muted") } },
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            var w = d.watts[i];
            return (d.wraps ? "Night of " : "") + labels[i] + "\n" +
              (w === null ? "no figure: half-hours missing in the window" : w.toLocaleString("en-GB", { maximumFractionDigits: 0 }) + " W");
          },
        }),
        legend: { show: false },
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "W", min: 0 }, axisStyle()),
        series: [{ name: name, type: "line", data: d.watts, connectNulls: false, showSymbol: d.labels.length < 60, itemStyle: { color: colours.electricity } }],
      });
    },
    tariffs: function (d) {
      // Usage and standing charge stacked, one bar per tariff; the first bar is what OVO charged.
      var fuelColour = colours[d.fuel];
      return Object.assign(base(), {
        title: { text: "Total over " + d.days + " days (£)", left: "right", textStyle: { fontSize: 12, color: token("--muted") } },
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return d.labels[i] + "\nUsage: " + pounds(d.usage[i]) + "\nStanding charge: " + pounds(d.standing[i]) + "\nTotal: " + pounds(d.total[i]);
          },
        }),
        xAxis: Object.assign({ type: "category", data: d.labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "£" }, axisStyle()),
        series: [
          { name: "Usage", type: "bar", stack: "cost", data: d.usage.map(money), itemStyle: { color: fuelColour } },
          { name: "Standing charge", type: "bar", stack: "cost", data: d.standing.map(money), itemStyle: { color: colours.standing } },
        ],
      });
    },
    readings: function (d) {
      var labels = d.labels.map(function (l) { return dateLabel(l, "day"); });
      // Windows the page flags are drawn as larger red points, on top of the line.
      function flagged(values) {
        var points = [];
        d.flagged.forEach(function (f, i) { if (f) points.push([labels[i], values[i]]); });
        return { name: "Flagged", type: "scatter", data: points, symbolSize: 11, z: 5, itemStyle: { color: "#d1432f" } };
      }
      if (d.kind === "gas") {
        return Object.assign(base(), {
          tooltip: Object.assign(base().tooltip, {
            formatter: function (p) {
              var i = p[0].dataIndex;
              return "To " + labels[i] + " (" + d.days[i] + " days)\nImplied calorific value: " + d.implied[i] + " MJ/m³" +
                (d.flagged[i] ? "\n(outside the range on the bill)" : "");
            },
          }),
          legend: { show: false },
          xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
          yAxis: Object.assign({ type: "value", name: "MJ/m³", min: function (v) { return Math.floor(Math.min(v.min, d.low) - 1); }, max: function (v) { return Math.ceil(Math.max(v.max, d.high) + 1); } }, axisStyle()),
          series: [{
            name: "Implied calorific value", type: "line", data: d.implied, symbolSize: 6, itemStyle: { color: colours.gas },
            markLine: { silent: true, symbol: "none", lineStyle: { type: "dashed", color: token("--muted") }, label: { color: token("--muted") }, data: [{ yAxis: d.low, name: "bill low" }, { yAxis: d.high, name: "bill high" }] },
          }, flagged(d.implied)],
        });
      }
      return Object.assign(base(), {
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) {
            var i = p[0].dataIndex;
            return "To " + labels[i] + " (" + d.days[i] + " days)\nFrom readings: " + kwh(d.derived[i]) + "\nSmart data: " + kwh(d.smart[i]);
          },
        }),
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "kWh" }, axisStyle()),
        series: [
          { name: "From readings", type: "line", data: d.derived, showSymbol: d.labels.length < 60, itemStyle: { color: colours.electricity } },
          { name: "Smart data", type: "line", data: d.smart, showSymbol: false, lineStyle: { type: "dashed" }, itemStyle: { color: colours.previous } },
          flagged(d.derived),
        ],
      });
    },
    gas: function (d) {
      var labels = d.labels.map(function (l) { return dateLabel(l, "day"); });
      return Object.assign(base(), {
        tooltip: Object.assign(base().tooltip, {
          formatter: function (p) { return labels[p[0].dataIndex] + "\n" + kwh(d.kwh[p[0].dataIndex]); },
        }),
        legend: { show: false },
        xAxis: Object.assign({ type: "category", data: labels }, axisStyle()),
        yAxis: Object.assign({ type: "value", name: "kWh" }, axisStyle()),
        series: [{ type: "bar", data: d.kwh, itemStyle: { color: colours.gas } }],
      });
    },
  };

  function fail(el) {
    el.textContent = "";
    var p = document.createElement("p");
    p.className = "chart-error";
    p.textContent = "This chart couldn't be loaded. Reload the page to try again.";
    el.appendChild(p);
  }

  function draw(el) {
    var build = builders[el.getAttribute("data-chart")];
    if (!build || !window.echarts) { fail(el); return; }
    fetch(el.getAttribute("data-src"), { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.json(); })
      .then(function (data) {
        var chart = window.echarts.init(el, null, { renderer: "canvas" });
        chart.setOption(build(data));
        window.addEventListener("resize", function () { chart.resize(); });
      })
      .catch(function () { fail(el); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-chart]").forEach(draw);
  });
})();
