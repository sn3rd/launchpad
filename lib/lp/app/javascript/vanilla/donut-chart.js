/**
 * Donut chart renderer. Finds <svg chart='donut'>..</svg> elements and draws a
 * d3-powered donut chart inside each one, reading configuration from
 * HTML attributes.
 *
 * HTML attributes:
 *   chart-segments   – JSON array of { value: number, fill: string, label?: string }
 *   chart-clockwise  – "false" to draw counter-clockwise (default: true)
 *   chart-radius     – outer radius in px (default: 100)
 *   chart-thickness  – ring thickness in px; innerRadius = radius − thickness
 *                      (default: 25)
 *   chart-gap        – gap between segments as a ratio (0..1) of the
 *                      full circle (default: 0.004)
 *   chart-min-ratio  – minimum share (0..1) any segment should occupy;
 *                      values below this are grown and the rest are
 *                      scaled down to compensate (default: 0)
 *   chart-hover-grow – px each segment grows outward on hover
 *                      (default: 4, set to 0 to disable)
 *   chart-title      – optional text rendered in the center of the donut
 *                      (default: "")
 *   chart-subtitle   – optional text rendered below the title
 *                      (default: "")
 *
 * State attributes (written by JS):
 *   data-donut-initialized – present on charts that have been rendered;
 *                            used to keep initDonutCharts idempotent.
 *
 */

/**
 * @typedef {Object} DonutChartSegment
 * @property {number} value - The value of the segment.
 * @property {string} fill - The CSS fill color of the segment.
 * @property {string} [label] - Optional tooltip label shown on hover.
 *
 * @typedef {DonutChartSegment[]} DonutChartSegments
 *
 * @typedef {Object} DonutChartOptions
 * @property {boolean} clockwise - Whether the donut chart should be drawn clockwise.
 * @property {number} radius - The donut chart radius in pixels.
 * @property {number} thickness - The donut chart segment thickness in pixels.
 * @property {number} gap - The gap between segments as a ratio (0..1) of the full circle.
 * @property {number} minRatio - The minimum ratio (0..1) any segment should occupy.
 * @property {number} hoverGrow - How many pixels segments grow outward on hover.
 * @property {string} [title] - Optional text rendered in the center of the donut.
 * @property {string} [subtitle] - Optional text rendered below the title.
 */

export const SELECTOR = "svg[chart='donut']"

export const ATTRIBUTES = {
  /**
   * JSON array of segments in {@link DonutChartSegments} format.
   */
  chartSegments: "chart-segments",
  /**
   * Whether the donut chart should be drawn clockwise.
   * @default true
   */
  chartClockwise: "chart-clockwise",
  /**
   * The donut chart outer radius in pixels.
   * @default 100
   */
  chartRadius: "chart-radius",
  /**
   * The donut chart ring thickness in pixels.
   * innerRadius = radius − thickness.
   * @default 25
   */
  chartThickness: "chart-thickness",
  /**
   * The gap between segments as a ratio (0..1) of the full circle.
   * Clamped jointly with min-ratio to fit the circle.
   * @default 0.004
   */
  chartGap: "chart-gap",
  /**
   * Minimum ratio (0..1) any segment should occupy.
   * Segments below this are grown; others are scaled down to compensate.
   * @default 0
   */
  chartMinRatio: "chart-min-ratio",
  /**
   * How many pixels a segment grows outward on hover.
   * Set to 0 to disable the hover effect.
   * @default 4
   */
  chartHoverGrow: "chart-hover-grow",
  /**
   * Optional text rendered in the center of the donut.
   * @default ""
   */
  chartTitle: "chart-title",
  /**
   * Optional text rendered below the title.
   * @default ""
   */
  chartSubtitle: "chart-subtitle",

  /**
   * Marker set by JS on charts that have already been rendered, so
   * {@link initDonutCharts} stays idempotent.
   */
  chartInitialized: "data-donut-initialized",
}

// -- Tooltip --

let sharedTooltip = null

// source: https://vanillaframework.io/docs/patterns/tooltips#detached
function getSharedTooltip() {
  if (sharedTooltip && document.body.contains(sharedTooltip)) return sharedTooltip

  const container = document.createElement("div")
  container.className = "p-tooltip is-detached u-hide"
  container.setAttribute("data-donut-tooltip", "")
  // Absolute positioning so we can follow the mouse in page coordinates.
  container.style.position = "absolute"
  container.style.pointerEvents = "none"
  container.style.zIndex = "1000"

  const message = document.createElement("span")
  message.className = "p-tooltip__message"
  message.setAttribute("role", "tooltip")
  container.appendChild(message)

  document.body.appendChild(container)
  sharedTooltip = container
  return container
}

function showTooltip(label, x, y) {
  const tooltip = getSharedTooltip()
  const message = tooltip.querySelector(".p-tooltip__message")
  message.textContent = label
  tooltip.classList.remove("u-hide")
  moveTooltip(x, y)
}

function moveTooltip(x, y) {
  if (!sharedTooltip) return
  // Offset a bit from the cursor so it doesn't sit under the pointer.
  const offsetX = 0
  const offsetY = 10
  sharedTooltip.style.left = `${x + window.scrollX + offsetX}px`
  sharedTooltip.style.top = `${y + window.scrollY + offsetY}px`
}

function hideTooltip() {
  if (!sharedTooltip) return
  sharedTooltip.classList.add("u-hide")
}

function hasFocusedSegment() {
  return document.querySelector(".segment:focus") !== null
}

/**
 * Wires hover + keyboard focus tooltip behavior for a labelled segment.
 * Keyboard focus wins: while any segment on the page is focused, mouse hover
 * events on segments are ignored — the shared tooltip belongs to the focused
 * segment regardless of which chart it lives in. On blur, the tooltip is
 * handed back to whichever segment the mouse is currently over (across all
 * charts).
 *
 * @param {SVGSVGElement} svg the SVG element containing the segments
 * @param {SVGPathElement} path the segment element to show the tooltip for
 * @param {string} label the tooltip label to show
 * @param {[number, number]} centroid [x, y] centroid of the segment in SVG local coordinates
 *   (e.g. `arc.centroid(pieData)`)
 */
export function initSegmentTooltip(svg, path, label, centroid) {
  path.setAttribute("tabindex", "0")
  path.setAttribute("role", "img")
  path.setAttribute("aria-label", label)

  path.addEventListener("mouseenter", (e) => {
    if (hasFocusedSegment()) return
    showTooltip(label, e.clientX, e.clientY)
  })
  path.addEventListener("mousemove", (e) => {
    if (hasFocusedSegment()) return
    moveTooltip(e.clientX, e.clientY)
  })
  path.addEventListener("mouseleave", () => {
    if (hasFocusedSegment()) return
    hideTooltip()
  })
  path.addEventListener("focus", () => {
    // Centroid is in SVG local coords; transform to viewport coords so the
    // tooltip (positioned in page coords)
    const pt = svg.createSVGPoint()
    pt.x = centroid[0]
    pt.y = centroid[1]
    const screen = pt.matrixTransform(path.getScreenCTM())
    showTooltip(label, screen.x, screen.y)
  })
  path.addEventListener("blur", () => {
    // If the mouse is over a segment (in any chart), hand the tooltip to it.
    const hovered = document.querySelector(".segment:hover")
    if (hovered instanceof Element) {
      const hoveredLabel = hovered.getAttribute("aria-label")
      const rect = hovered.getBoundingClientRect()
      if (hoveredLabel) {
        showTooltip(hoveredLabel, rect.left + rect.width / 2, rect.top + rect.height / 2)
        return
      }
    }
    hideTooltip()
  })
}


// -- Utils --

function parseAttrInt(attrValue, defaultValue) {
  if (attrValue == null) return defaultValue
  const value = parseInt(attrValue)
  return isNaN(value) ? defaultValue : value
}

function parseAttrFloat(attrValue, defaultValue) {
  if (attrValue == null) return defaultValue
  const value = parseFloat(attrValue)
  return isNaN(value) ? defaultValue : value
}

function parseAttrBool(attrValue, defaultValue) {
  if (attrValue == null) return defaultValue
  return attrValue !== "false"
}

/**
 * Redistributes values to ensure each segment has at least `minRatio` of the total.
 * 
 * source: launchpad-ui:src/lib/components/PieChart/utils
 * @param {number[]} values
 * @param {number} minRatio
 * @returns {number[]}
 */
function applyMinRatio(values, minRatio) {
  if (minRatio <= 0 || values.length === 0) return values
  const total = values.reduce((a, b) => a + b, 0)
  if (total <= 0) return values

  const expectedMinValue = total * minRatio
  const belowMin = values.filter((v) => v < expectedMinValue)
  if (belowMin.length === 0) return values

  const aboveMin = values.filter((v) => v >= expectedMinValue)
  if (aboveMin.length === 0) return values

  const belowMinTotal = belowMin.reduce((a, b) => a + b, 0)
  const aboveMinTotal = aboveMin.reduce((a, b) => a + b, 0)

  const deficit = belowMin.length * expectedMinValue - belowMinTotal
  const scale = (aboveMinTotal - deficit) / aboveMinTotal

  return values.map((v) =>
    v < expectedMinValue ? expectedMinValue : v * scale,
  )
}

/**
 * Clamps gap and minRatio so they fit within the chart's angular range.
 *
 * Both `gap` and `minRatio` are ratios (0..1) of the full circle.
 *
 * source: launchpad-ui:src/lib/components/PieChart/utils
 * @param {number} segmentCount
 * @param {number} gap
 * @param {number} minRatio
 * @returns {{ gap: number, minRatio: number }}
 */
function clampPieConstraints(segmentCount, gap, minRatio) {
  if (segmentCount === 0) return { gap: 0, minRatio: 0 }

  const gapSpace = gap * segmentCount
  const minRatioSpace = minRatio * segmentCount

  if (gapSpace + minRatioSpace <= 1) return { gap, minRatio }

  const maxGapRatio = 1 / (2 * segmentCount)
  const clampedGap = Math.min(gap, maxGapRatio)

  const maxMinRatio = 1 / (2 * segmentCount)
  const clampedMinRatio = Math.min(minRatio, maxMinRatio)

  return { gap: clampedGap, minRatio: clampedMinRatio }
}

// -- Renderer --

/**
 * Draws a donut chart into the given SVG element.
 * @param {SVGSVGElement} svg
 * @param {DonutChartSegments} segments
 * @param {DonutChartOptions} options
 */
export function drawDonutChart(svg, segments, options) {
  const {
    clockwise,
    radius: rawRadius,
    thickness,
    gap,
    minRatio,
    hoverGrow,
    title,
    subtitle,
  } = options

  // clear any previous render
  svg.replaceChildren()

  const radius = Math.max(0, rawRadius)
  const innerRadius = Math.max(0, radius - thickness)
  const padding = Math.max(0, hoverGrow)
  const size = radius * 2 + padding * 2
  svg.setAttribute("viewBox", `${-size / 2} ${-size / 2} ${size} ${size}`)
  svg.setAttribute("width", svg.getAttribute("width") ?? String(size))
  svg.setAttribute("height", svg.getAttribute("height") ?? String(size))

  if (!segments || segments.length === 0) return

  const clamped = clampPieConstraints(segments.length, gap, minRatio)

  const adjustedValues = applyMinRatio(
    segments.map((s) => s.value),
    clamped.minRatio,
  )
  const adjustedSegments = adjustedValues.map((v, i) => ({
    ...segments[i],
    value: v,
  }))

  const startAngle = 0
  const endAngle = startAngle + (clockwise ? 2 * Math.PI : -2 * Math.PI)

  const pie = d3
    .pie()
    .value((d) => d.value)
    .sort(null)
    .startAngle(startAngle)
    .endAngle(endAngle)

  const padAngle = clamped.gap * 2 * Math.PI

  const arc = d3
    .arc()
    .innerRadius(innerRadius)
    .outerRadius(radius)
    .padAngle(padAngle)

  const hoverArc = d3
    .arc()
    .innerRadius(innerRadius)
    .outerRadius(radius + padding)
    .padAngle(padAngle)

  const arcs = pie(adjustedSegments).reverse()

  d3.select(svg)
    .selectAll("path.segment")
    .data(arcs)
    .join("path")
    .attr("class", "segment")
    .attr("d", (a) => arc(a) ?? "")
    .attr("fill", (a) => a.data.fill)
    .each(function (a) {
      const baseD = arc(a) ?? ""
      const hoverD = hoverArc(a) ?? ""
      this.style.setProperty("--arc-d-hover", `path("${hoverD || baseD}")`)
      if (a.data.label) {
        initSegmentTooltip(svg, this, a.data.label, arc.centroid(a))
      }
    })

  const svgNS = "http://www.w3.org/2000/svg"

  if (title) {
    const text = document.createElementNS(svgNS, "text")
    text.classList.add("title")
    text.textContent = title
    svg.appendChild(text)
  }

  if (subtitle) {
    const text = document.createElementNS(svgNS, "text")
    text.classList.add("subtitle")
    text.textContent = subtitle
    svg.appendChild(text)
  }
}

/**
 * Initializes the donut charts in the given root element.
 * Idempotent — already-initialized charts are skipped.
 * @param {HTMLElement | Document} root
 */
export function initDonutCharts(root) {
  /** @type {SVGSVGElement[]} */
  const donutCharts = Array.from(root.querySelectorAll(SELECTOR))
  for (const donutChart of donutCharts) {
    if (donutChart.hasAttribute(ATTRIBUTES.chartInitialized)) continue

    const segmentsAttr = donutChart.getAttribute(ATTRIBUTES.chartSegments) ?? "[]"
    let segments
    try {
      segments = JSON.parse(segmentsAttr)
    } catch (err) {
      console.warn("donut-chart: invalid chart-segments JSON", donutChart, err)
      continue
    }

    const clockwise = parseAttrBool(
      donutChart.getAttribute(ATTRIBUTES.chartClockwise),
      true,
    )
    const radius = parseAttrInt(
      donutChart.getAttribute(ATTRIBUTES.chartRadius),
      100,
    )
    const thickness = parseAttrInt(
      donutChart.getAttribute(ATTRIBUTES.chartThickness),
      25,
    )
    const gap = parseAttrFloat(
      donutChart.getAttribute(ATTRIBUTES.chartGap),
      0.004,
    )
    const minRatio = parseAttrFloat(
      donutChart.getAttribute(ATTRIBUTES.chartMinRatio),
      0,
    )
    const hoverGrow = parseAttrInt(
      donutChart.getAttribute(ATTRIBUTES.chartHoverGrow),
      4,
    )
    const title = donutChart.getAttribute(ATTRIBUTES.chartTitle)
    const subtitle = donutChart.getAttribute(ATTRIBUTES.chartSubtitle)

    drawDonutChart(donutChart, segments, {
      clockwise,
      radius,
      thickness,
      gap,
      minRatio,
      hoverGrow,
      title,
      subtitle,
    })

    donutChart.setAttribute(ATTRIBUTES.chartInitialized, "")
  }
}

initDonutCharts(document)

document.addEventListener("swap:afterSwap", (e) => initDonutCharts(e.target));
