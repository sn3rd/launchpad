/**
 * Tests for lib/lp/app/javascript/vanilla/donut-chart.js
 */
import {
  SELECTOR,
  ATTRIBUTES,
  drawDonutChart,
  initDonutCharts,
} from "../donut-chart.js";

// -- Helpers --

const svgNS = "http://www.w3.org/2000/svg";

function makeSVG() {
  const svg = document.createElementNS(svgNS, "svg");
  document.body.appendChild(svg);
  return svg;
}

function defaultOptions(overrides) {
  return {
    clockwise: true,
    radius: 100,
    thickness: 25,
    gap: 0,
    minRatio: 0,
    hoverGrow: 4,
    title: "",
    subtitle: "",
    ...overrides,
  };
}

function paths(svg) {
  return svg.querySelectorAll("path.segment");
}

function tooltipEl() {
  return document.querySelector("[data-donut-tooltip]");
}

function tooltipVisible() {
  const el = tooltipEl();
  return el && !el.classList.contains("u-hide");
}

function tooltipText() {
  const el = tooltipEl();
  if (!el) return null;
  const msg = el.querySelector(".p-tooltip__message");
  return msg ? msg.textContent : null;
}

// -- Tests --

VanillaTest.suite("donutChart", (T) => {
  T.suite("SELECTOR", (T) => {
    T.test("matches svg elements with chart='donut'", () => {
      const svg = makeSVG();
      svg.setAttribute("chart", "donut");
      T.ok(svg.matches(SELECTOR), "matches donut chart SVG");
    });

    T.test("does not match svg without chart attribute", () => {
      const svg = makeSVG();
      T.ok(!svg.matches(SELECTOR), "plain SVG does not match");
    });

    T.test("does not match svg with different chart type", () => {
      const svg = makeSVG();
      svg.setAttribute("chart", "bar");
      T.ok(!svg.matches(SELECTOR), "bar chart SVG does not match");
    });
  });

  T.suite("drawDonutChart", (T) => {
    T.suite("empty and edge cases", (T) => {
      T.test("no segments produces no paths", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [], defaultOptions());
        T.equal(paths(svg).length, 0, "no path elements");
      });

      T.test("null segments produces no paths", () => {
        const svg = makeSVG();
        drawDonutChart(svg, null, defaultOptions());
        T.equal(paths(svg).length, 0, "no path elements");
      });

      T.test("clears previous render", () => {
        const svg = makeSVG();
        const existing = document.createElementNS(svgNS, "circle");
        svg.appendChild(existing);

        drawDonutChart(svg, [], defaultOptions());
        T.equal(svg.querySelectorAll("circle").length, 0, "old content removed");
      });
    });

    T.suite("segments", (T) => {
      T.test("single segment draws one path", () => {
        const svg = makeSVG();
        const segments = [{ value: 10, fill: "red" }];
        drawDonutChart(svg, segments, defaultOptions());
        T.equal(paths(svg).length, 1, "one path");
      });

      T.test("multiple segments draw correct number of paths", () => {
        const svg = makeSVG();
        const segments = [
          { value: 30, fill: "red" },
          { value: 50, fill: "blue" },
          { value: 20, fill: "green" },
        ];
        drawDonutChart(svg, segments, defaultOptions());
        T.equal(paths(svg).length, 3, "three paths");
      });

      T.test("fill colors are applied from segment data", () => {
        const svg = makeSVG();
        const segments = [
          { value: 1, fill: "#ff0000" },
          { value: 1, fill: "#00ff00" },
        ];
        drawDonutChart(svg, segments, defaultOptions());
        const p = paths(svg);
        // Clockwise (default) reverses DOM order so visual order matches
        T.equal(p[0].getAttribute("fill"), "#00ff00", "first DOM path is last segment");
        T.equal(p[1].getAttribute("fill"), "#ff0000", "second DOM path is first segment");
      });

      T.test("paths have d attribute set", () => {
        const svg = makeSVG();
        const segments = [{ value: 10, fill: "red" }];
        drawDonutChart(svg, segments, defaultOptions());
        const d = paths(svg)[0].getAttribute("d");
        T.ok(d, "d attribute is set");
        T.ok(d.length > 0, "d attribute is non-empty");
      });

      T.test("paths have --arc-d-hover CSS variable", () => {
        const svg = makeSVG();
        const segments = [{ value: 10, fill: "red" }];
        drawDonutChart(svg, segments, defaultOptions());
        const hoverVar = paths(svg)[0].style.getPropertyValue("--arc-d-hover");
        T.ok(hoverVar, "hover CSS variable is set");
      });

      T.test("zero-value segments are rendered", () => {
        const svg = makeSVG();
        const segments = [
          { value: 0, fill: "red" },
          { value: 10, fill: "blue" },
        ];
        drawDonutChart(svg, segments, defaultOptions());
        T.equal(paths(svg).length, 2, "both segments rendered");
      });
    });

    T.suite("viewBox and sizing", (T) => {
      T.test("viewBox reflects radius and hoverGrow", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          radius: 50,
          hoverGrow: 5,
        }));
        const size = 50 * 2 + 5 * 2; // 110
        T.equal(
          svg.getAttribute("viewBox"),
          `${-size / 2} ${-size / 2} ${size} ${size}`,
          "viewBox calculated correctly"
        );
        T.equal(svg.getAttribute("width"), String(size), "width set");
        T.equal(svg.getAttribute("height"), String(size), "height set");
      });

      T.test("hoverGrow 0 means no padding", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          radius: 100,
          hoverGrow: 0,
        }));
        T.equal(svg.getAttribute("width"), "200", "width = 2 * radius");
        T.equal(svg.getAttribute("height"), "200", "height = 2 * radius");
      });

      T.test("radius 0 produces size 0 plus padding", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [], defaultOptions({ radius: 0, hoverGrow: 0 }));
        T.equal(svg.getAttribute("width"), "0", "width is 0");
        T.equal(svg.getAttribute("height"), "0", "height is 0");
      });
    });

    T.suite("title and subtitle", (T) => {
      T.test("renders title text element", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          title: "42",
        }));
        const titles = svg.querySelectorAll("text.title");
        T.equal(titles.length, 1, "one title element");
        T.equal(titles[0].textContent, "42", "title text");
      });

      T.test("renders subtitle text element", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          subtitle: "packages",
        }));
        const subs = svg.querySelectorAll("text.subtitle");
        T.equal(subs.length, 1, "one subtitle element");
        T.equal(subs[0].textContent, "packages", "subtitle text");
      });

      T.test("no title when empty string", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          title: "",
        }));
        T.equal(svg.querySelectorAll("text.title").length, 0, "no title");
      });

      T.test("no subtitle when empty string", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          subtitle: "",
        }));
        T.equal(svg.querySelectorAll("text.subtitle").length, 0, "no subtitle");
      });

      T.test("title and subtitle together", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions({
          title: "100",
          subtitle: "total",
        }));
        T.equal(svg.querySelectorAll("text.title").length, 1, "has title");
        T.equal(svg.querySelectorAll("text.subtitle").length, 1, "has subtitle");
      });
    });

    T.suite("direction", (T) => {
      T.test("clockwise and counter-clockwise produce different paths", () => {
        const segments = [
          { value: 30, fill: "red" },
          { value: 70, fill: "blue" },
        ];

        const svgCW = makeSVG();
        drawDonutChart(svgCW, segments, defaultOptions({ clockwise: true }));
        const cwPath = paths(svgCW)[0].getAttribute("d");

        const svgCCW = makeSVG();
        drawDonutChart(svgCCW, segments, defaultOptions({ clockwise: false }));
        const ccwPath = paths(svgCCW)[0].getAttribute("d");

        T.ok(cwPath !== ccwPath, "paths differ between CW and CCW");
      });
    });

    T.suite("gap", (T) => {
      T.test("gap produces different paths than no gap", () => {
        const segments = [
          { value: 50, fill: "red" },
          { value: 50, fill: "blue" },
        ];

        const svgNoGap = makeSVG();
        drawDonutChart(svgNoGap, segments, defaultOptions({ gap: 0 }));
        const noGapPath = paths(svgNoGap)[0].getAttribute("d");

        const svgGap = makeSVG();
        drawDonutChart(svgGap, segments, defaultOptions({ gap: 5 }));
        const gapPath = paths(svgGap)[0].getAttribute("d");

        T.ok(noGapPath !== gapPath, "gap changes path geometry");
      });
    });

    T.suite("minRatio", (T) => {
      T.test("minRatio does not reduce segment count", () => {
        const svg = makeSVG();
        const segments = [
          { value: 1, fill: "red" },
          { value: 99, fill: "blue" },
        ];
        drawDonutChart(svg, segments, defaultOptions({ minRatio: 0.1 }));
        T.equal(paths(svg).length, 2, "both segments still present");
      });

      T.test("minRatio changes path of small segment", () => {
        const segments = [
          { value: 1, fill: "red" },
          { value: 99, fill: "blue" },
        ];

        const svgNoMin = makeSVG();
        drawDonutChart(svgNoMin, segments, defaultOptions({ minRatio: 0 }));
        const noMinPath = paths(svgNoMin)[0].getAttribute("d");

        const svgMin = makeSVG();
        drawDonutChart(svgMin, segments, defaultOptions({ minRatio: 0.1 }));
        const minPath = paths(svgMin)[0].getAttribute("d");

        T.ok(noMinPath !== minPath, "minRatio enlarges small segment");
      });
    });

    T.suite("label and accessibility", (T) => {
      T.test("segment with label gets tabindex and role", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "Red" }], defaultOptions());
        const p = paths(svg)[0];
        T.equal(p.getAttribute("tabindex"), "0", "tabindex set");
        T.equal(p.getAttribute("role"), "img", "role set");
        T.equal(p.getAttribute("aria-label"), "Red", "aria-label set");
      });

      T.test("segment without label has no tabindex or role", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions());
        const p = paths(svg)[0];
        T.ok(!p.hasAttribute("tabindex"), "no tabindex");
        T.ok(!p.hasAttribute("role"), "no role");
        T.ok(!p.hasAttribute("aria-label"), "no aria-label");
      });

      T.test("mixed segments: only labelled ones get accessibility attrs", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [
          { value: 1, fill: "red", label: "Red" },
          { value: 1, fill: "blue" },
        ], defaultOptions());
        const p = paths(svg);
        // Clockwise reverses DOM order: p[0] is the unlabelled segment
        T.ok(!p[0].hasAttribute("aria-label"), "unlabelled segment has no aria-label");
        T.ok(p[1].hasAttribute("aria-label"), "labelled segment has aria-label");
      });
    });

    T.suite("tooltip", (T) => {
      T.test("mouseenter shows tooltip with label text", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "Red slice" }], defaultOptions());
        const p = paths(svg)[0];

        p.dispatchEvent(new MouseEvent("mouseenter", { clientX: 50, clientY: 60, bubbles: true }));
        T.ok(tooltipVisible(), "tooltip is visible");
        T.equal(tooltipText(), "Red slice", "tooltip shows label");
      });

      T.test("mouseleave hides tooltip", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "Red" }], defaultOptions());
        const p = paths(svg)[0];

        p.dispatchEvent(new MouseEvent("mouseenter", { clientX: 10, clientY: 10, bubbles: true }));
        T.ok(tooltipVisible(), "visible after enter");

        p.dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
        T.ok(!tooltipVisible(), "hidden after leave");
      });

      T.test("mousemove repositions tooltip", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "R" }], defaultOptions());
        const p = paths(svg)[0];

        p.dispatchEvent(new MouseEvent("mouseenter", { clientX: 10, clientY: 20, bubbles: true }));
        const leftBefore = tooltipEl().style.left;

        p.dispatchEvent(new MouseEvent("mousemove", { clientX: 100, clientY: 200, bubbles: true }));
        const leftAfter = tooltipEl().style.left;

        T.ok(leftBefore !== leftAfter, "tooltip moved on mousemove");
      });

      T.test("focus shows tooltip, blur hides it", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "Focused" }], defaultOptions());
        const p = paths(svg)[0];

        p.dispatchEvent(new Event("focus", { bubbles: true }));
        T.ok(tooltipVisible(), "visible on focus");
        T.equal(tooltipText(), "Focused", "correct text on focus");

        p.dispatchEvent(new Event("blur", { bubbles: true }));
        T.ok(!tooltipVisible(), "hidden on blur");
      });

      T.test("no tooltip for segments without label", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions());
        const p = paths(svg)[0];

        p.dispatchEvent(new MouseEvent("mouseenter", { clientX: 10, clientY: 10, bubbles: true }));
        T.ok(!tooltipVisible(), "tooltip not shown");
      });

      T.test("tooltip is shared across segments", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [
          { value: 1, fill: "red", label: "Red" },
          { value: 1, fill: "blue", label: "Blue" },
        ], defaultOptions());
        const p = paths(svg);
        // Clockwise reverses DOM order: p[0]=Blue, p[1]=Red

        p[0].dispatchEvent(new MouseEvent("mouseenter", { clientX: 10, clientY: 10, bubbles: true }));
        T.equal(tooltipText(), "Blue", "shows first DOM segment label");

        p[0].dispatchEvent(new MouseEvent("mouseleave", { bubbles: true }));
        p[1].dispatchEvent(new MouseEvent("mouseenter", { clientX: 20, clientY: 20, bubbles: true }));
        T.equal(tooltipText(), "Red", "shows second DOM segment label");

        // Only one tooltip element in the DOM
        T.equal(
          document.querySelectorAll("[data-donut-tooltip]").length,
          1,
          "single shared tooltip element"
        );
      });

      T.test("tooltip element has expected structure", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red", label: "X" }], defaultOptions());
        paths(svg)[0].dispatchEvent(new MouseEvent("mouseenter", { clientX: 0, clientY: 0, bubbles: true }));

        const tip = tooltipEl();
        T.ok(tip.classList.contains("p-tooltip"), "has p-tooltip class");
        T.ok(tip.classList.contains("is-detached"), "has is-detached class");
        T.equal(tip.style.position, "absolute", "absolutely positioned");
        T.equal(tip.style.pointerEvents, "none", "pointer-events none");
        const msg = tip.querySelector(".p-tooltip__message");
        T.ok(msg, "has message element");
        T.equal(msg.getAttribute("role"), "tooltip", "message has role=tooltip");
      });
    });

    T.suite("re-render", (T) => {
      T.test("calling drawDonutChart twice replaces content", () => {
        const svg = makeSVG();
        drawDonutChart(svg, [{ value: 1, fill: "red" }], defaultOptions());
        T.equal(paths(svg).length, 1, "one path after first render");

        drawDonutChart(svg, [
          { value: 1, fill: "red" },
          { value: 1, fill: "blue" },
        ], defaultOptions());
        T.equal(paths(svg).length, 2, "two paths after re-render");
      });
    });
  });

  T.suite("initDonutCharts", (T) => {
    T.test("initializes chart from HTML attributes", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 60, fill: "red" },
        { value: 40, fill: "blue" },
      ]));
      svg.setAttribute("chart-radius", "50");
      svg.setAttribute("chart-thickness", "10");
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(paths(svg).length, 2, "segments rendered");
      T.ok(
        svg.hasAttribute(ATTRIBUTES.chartInitialized),
        "initialized marker set"
      );
    });

    T.test("is idempotent — skips already initialized charts", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
      ]));
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);
      T.equal(paths(svg).length, 1, "rendered once");

      // Change segments attribute — should NOT re-render
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
        { value: 1, fill: "blue" },
      ]));
      initDonutCharts(container);
      T.equal(paths(svg).length, 1, "still one path after second init");
    });

    T.test("uses default values when attributes are missing", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
      ]));
      // No other attributes — defaults should apply
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(paths(svg).length, 1, "rendered with defaults");
      // Default radius=100, hoverGrow=4 → size = 208
      T.equal(svg.getAttribute("width"), "208", "default sizing applied");
    });

    T.test("parses chart-clockwise='false'", () => {
      const container = document.createElement("div");
      const segments = [
        { value: 30, fill: "red" },
        { value: 70, fill: "blue" },
      ];

      const svgCW = document.createElementNS(svgNS, "svg");
      svgCW.setAttribute("chart", "donut");
      svgCW.setAttribute("chart-segments", JSON.stringify(segments));
      svgCW.setAttribute("chart-clockwise", "true");
      container.appendChild(svgCW);

      const svgCCW = document.createElementNS(svgNS, "svg");
      svgCCW.setAttribute("chart", "donut");
      svgCCW.setAttribute("chart-segments", JSON.stringify(segments));
      svgCCW.setAttribute("chart-clockwise", "false");
      container.appendChild(svgCCW);

      document.body.appendChild(container);
      initDonutCharts(container);

      const cwPath = paths(svgCW)[0].getAttribute("d");
      const ccwPath = paths(svgCCW)[0].getAttribute("d");
      T.ok(cwPath !== ccwPath, "clockwise=false produces different path");
    });

    T.test("parses chart-title and chart-subtitle", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
      ]));
      svg.setAttribute("chart-title", "42");
      svg.setAttribute("chart-subtitle", "packages");
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(
        svg.querySelector("text.title").textContent,
        "42",
        "title parsed"
      );
      T.equal(
        svg.querySelector("text.subtitle").textContent,
        "packages",
        "subtitle parsed"
      );
    });

    T.test("handles empty segments JSON", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", "[]");
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(paths(svg).length, 0, "no paths for empty segments");
      T.ok(
        svg.hasAttribute(ATTRIBUTES.chartInitialized),
        "still marked as initialized"
      );
    });

    T.test("initializes multiple charts in one call", () => {
      const container = document.createElement("div");

      const svg1 = document.createElementNS(svgNS, "svg");
      svg1.setAttribute("chart", "donut");
      svg1.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
      ]));
      container.appendChild(svg1);

      const svg2 = document.createElementNS(svgNS, "svg");
      svg2.setAttribute("chart", "donut");
      svg2.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "blue" },
        { value: 1, fill: "green" },
      ]));
      container.appendChild(svg2);

      document.body.appendChild(container);
      initDonutCharts(container);

      T.equal(paths(svg1).length, 1, "first chart rendered");
      T.equal(paths(svg2).length, 2, "second chart rendered");
    });

    T.test("parses chart-gap and chart-min-ratio", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
        { value: 99, fill: "blue" },
      ]));
      svg.setAttribute("chart-gap", "3");
      svg.setAttribute("chart-min-ratio", "0.1");
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(paths(svg).length, 2, "segments rendered with gap and minRatio");
    });

    T.test("parses chart-hover-grow='0' to disable hover padding", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      svg.setAttribute("chart-segments", JSON.stringify([
        { value: 1, fill: "red" },
      ]));
      svg.setAttribute("chart-radius", "50");
      svg.setAttribute("chart-hover-grow", "0");
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      // radius=50, hoverGrow=0 → size = 100
      T.equal(svg.getAttribute("width"), "100", "no hover padding");
    });

    T.test("missing chart-segments defaults to empty array", () => {
      const container = document.createElement("div");
      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("chart", "donut");
      // No chart-segments attribute
      container.appendChild(svg);
      document.body.appendChild(container);

      initDonutCharts(container);

      T.equal(paths(svg).length, 0, "no paths without segments");
      T.ok(
        svg.hasAttribute(ATTRIBUTES.chartInitialized),
        "still initialized"
      );
    });
  });
});
