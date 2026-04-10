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
 *   chart-gap        – gap between segments in px (default: 0)
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
 * @property {number} gap - The gap between segments in pixels.
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
   * The gap between segments in pixels.
   * Clamped jointly with min-ratio to fit the circle.
   * @default 25
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

function showTooltip(label, clientX, clientY) {
  const tooltip = getSharedTooltip()
  const message = tooltip.querySelector(".p-tooltip__message")
  message.textContent = label
  tooltip.classList.remove("u-hide")
  moveTooltip(clientX, clientY)
}

function moveTooltip(clientX, clientY) {
  if (!sharedTooltip) return
  // Offset a bit from the cursor so it doesn't sit under the pointer.
  const offsetX = 0
  const offsetY = 14
  sharedTooltip.style.left = `${clientX + window.scrollX + offsetX}px`
  sharedTooltip.style.top = `${clientY + window.scrollY + offsetY}px`
}

function hideTooltip() {
  if (!sharedTooltip) return
  sharedTooltip.classList.add("u-hide")
}


// -- Utils --
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
 * source: launchpad-ui:src/lib/components/PieChart/utils
 * @param {number} segmentCount
 * @param {number} radius
 * @param {number} gap
 * @param {number} minRatio
 * @returns {{ gap: number, minRatio: number }}
 */
function clampPieConstraints(segmentCount, radius, gap, minRatio) {
  if (segmentCount === 0) return { gap: 0, minRatio: 0 }
  if (radius <= 0) return { gap, minRatio }

  const gapAngle = gap / radius
  const gapSpace = gapAngle * segmentCount
  const minRatioSpace = minRatio * 2 * Math.PI * segmentCount

  if (gapSpace + minRatioSpace <= 2 * Math.PI) return { gap, minRatio }

  const maxGapAngle = Math.PI / segmentCount
  const clampedGapAngle = Math.min(gapAngle, maxGapAngle)
  const clampedGap = clampedGapAngle * radius

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
  while (svg.firstChild) svg.removeChild(svg.firstChild)

  const radius = Math.max(0, rawRadius)
  const innerRadius = Math.max(0, radius - thickness)
  const padding = Math.max(0, hoverGrow)
  const size = radius * 2 + padding * 2
  svg.setAttribute("viewBox", `${-size / 2} ${-size / 2} ${size} ${size}`)
  svg.setAttribute("width", svg.getAttribute("width") ?? String(size))
  svg.setAttribute("height", svg.getAttribute("height") ?? String(size))

  const svgNS = "http://www.w3.org/2000/svg"

  if (!segments || segments.length === 0) return

  const clamped = clampPieConstraints(segments.length, radius, gap, minRatio)

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

  const padAngle = radius > 0 ? clamped.gap / radius : 0

  const arc = d3
    .arc()
    .innerRadius(innerRadius)
    .outerRadius(radius)
    .padRadius(clamped.gap)
    .padAngle(padAngle)

  const hoverArc = d3
    .arc()
    .innerRadius(innerRadius)
    .outerRadius(radius + padding)
    .padRadius(clamped.gap)
    .padAngle(padAngle)

  const arcs = pie(adjustedSegments)

  for (const a of arcs) {
    /** @type {DonutChartSegment} */
    const data = a.data

    const path = document.createElementNS(svgNS, "path")
    const baseD = arc(a) ?? ""
    const hoverD = hoverArc(a) ?? ""
    path.classList.add("segment")
    path.setAttribute("d", baseD)
    path.setAttribute("fill", data.fill)
    path.style.setProperty("--arc-d-hover", `path("${hoverD ? hoverD : baseD}")`)

    if (data.label) {
      // Make the segment keyboard-focusable & accessible
      path.setAttribute("tabindex", "0")
      path.setAttribute("role", "img")
      path.setAttribute("aria-label", data.label)

      path.addEventListener("mouseenter", (e) => {
        showTooltip(data.label, e.clientX, e.clientY)
      })
      path.addEventListener("mousemove", (e) => {
        moveTooltip(e.clientX, e.clientY)
      })
      path.addEventListener("mouseleave", () => {
        hideTooltip()
      })
      path.addEventListener("focus", () => {
        const rect = path.getBoundingClientRect()
        showTooltip(data.label, rect.left + rect.width / 2, rect.top + rect.height / 2)
      })
      path.addEventListener("blur", () => {
        hideTooltip()
      })
    }

    svg.appendChild(path)
  }

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
  const parseAttrInt = (attrValue, defaultValue) => {
    if (attrValue == null) return defaultValue
    const value = parseInt(attrValue)
    return isNaN(value) ? defaultValue : value
  }
  const parseAttrFloat = (attrValue, defaultValue) => {
    if (attrValue == null) return defaultValue
    const value = parseFloat(attrValue)
    return isNaN(value) ? defaultValue : value
  }
  const parseAttrBool = (attrValue, defaultValue) => {
    if (attrValue == null) return defaultValue
    return attrValue !== "false"
  }

  /** @type {SVGSVGElement[]} */
  const donutCharts = Array.from(root.querySelectorAll(SELECTOR))
  for (const donutChart of donutCharts) {
    if (donutChart.hasAttribute(ATTRIBUTES.chartInitialized)) continue

    const segments = JSON.parse(
      donutChart.getAttribute(ATTRIBUTES.chartSegments) ?? "[]",
    )
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
    const gap = parseAttrInt(donutChart.getAttribute(ATTRIBUTES.chartGap), 25)
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
