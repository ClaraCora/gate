---
name: Gate
description: A quiet regional exit calibration bench for one VPS operator.
colors:
  graphite-rail: "#0f172a"
  graphite-control: "#1e293b"
  workshop-canvas: "#f1f5f9"
  work-surface: "#ffffff"
  muted-surface: "#f8fafc"
  divider: "#e2e8f0"
  divider-strong: "#cbd5e1"
  text-primary: "#0f172a"
  text-muted: "#64748b"
  route-healthy: "#10b981"
  route-healthy-deep: "#047857"
  command-orange: "#f97316"
  command-orange-deep: "#ea580c"
  status-warning: "#f59e0b"
  status-danger: "#ef4444"
  focus-blue: "#3b82f6"
typography:
  headline:
    fontFamily: "Segoe UI Variable, Segoe UI, sans-serif"
    fontSize: "1.18rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0"
  body:
    fontFamily: "Segoe UI Variable, Segoe UI, sans-serif"
    fontSize: "0.78rem"
    fontWeight: 400
    lineHeight: 1.5
    letterSpacing: "0"
  label:
    fontFamily: "Segoe UI Variable, Segoe UI, sans-serif"
    fontSize: "0.7rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0"
  numeric:
    fontFamily: "Cascadia Mono, Consolas, monospace"
    fontSize: "0.74rem"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0"
rounded:
  protocol: "3px"
  compact: "4px"
  control: "6px"
  dialog: "8px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "24px"
  xl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.command-orange}"
    textColor: "#ffffff"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
    height: "38px"
  button-secondary:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.text-primary}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "8px 13px"
    height: "38px"
  port-socket:
    backgroundColor: "{colors.work-surface}"
    textColor: "{colors.text-primary}"
    typography: "{typography.numeric}"
    rounded: "5px"
    padding: "10px 12px"
---

# Design System: Gate

## Overview

**Creative North Star: "The Regional Calibration Bench"**

Gate looks like a compact instrument used repeatedly by one operator, not a general-purpose
analytics dashboard. A graphite command rail, numbered port sockets, hairline dividers, and an
explicit route trace make the fixed-entry/dynamic-exit model visible before any action is taken.

The interface is quiet, dense, and factual. Work surfaces are cool and nearly white; color is
reserved for command intent and operational state. Layout and copy must keep failures legible and
must never imply that an unavailable or unverified route is healthy.

**Key Characteristics:**

- Compact operational density with clear scan lines.
- Fixed SOCKS ports presented as physical sockets on one rail.
- Oxide orange for deliberate commands, teal for verified health, and red for exact failure.
- Tabular numerals and monospace endpoints wherever values must be compared.
- Flat work surfaces separated by borders, with elevation reserved for selection and overlays.

## Colors

The palette combines cool workshop neutrals with three sparse signals: orange for operator intent,
teal for verified routes, and red or amber for abnormal states.

### Primary

- **Oxide Command Orange:** Primary actions, selected port outlines, and the active tab underline.
- **Deep Oxide:** Hover state for primary actions; it must not become a decorative background.

### Secondary

- **Verified Route Teal:** Healthy tunnel lamps, live route wires, and successful event marks.
- **Deep Route Teal:** Strong healthy text and text selection where contrast needs more weight.

### Tertiary

- **Calibration Blue:** Keyboard focus only, keeping focus visible without borrowing a status color.
- **Warning Amber:** Work in progress, degraded routes, and queued or running jobs.
- **Failure Red:** Failed validation, unavailable exits, and destructive or blocking feedback.

### Neutral

- **Graphite Rail:** The command bar and active exit endpoint.
- **Workshop Canvas:** Page background beyond the bounded work surfaces.
- **Paper Work Surface:** Tables, detail bays, and ordinary controls.
- **Hairline Divider:** The structural grid that replaces decorative cards.

**The Sparse Signal Rule.** Saturated colors communicate command or state; they do not decorate
empty space.

**The Status Truth Rule.** Green or teal appears only after end-to-end validation has succeeded.

## Typography

**Display Font:** Segoe UI Variable, falling back to Segoe UI and system sans-serif.

**Body Font:** Segoe UI Variable, falling back to Segoe UI and system sans-serif.

**Label/Mono Font:** Cascadia Mono, falling back to Consolas and monospace.

**Character:** Neutral system typography keeps the application local, fast, and legible. The mono
face is a measuring instrument for ports, IP addresses, protocols, and timestamps rather than a
visual theme applied to prose.

### Hierarchy

- **Headline:** Compact, bold section names; never hero-scale inside the console.
- **Body:** Dense table and inspector copy with comfortable line height for failure explanations.
- **Label:** Small, bold headers and control labels designed for rapid scanning.
- **Numeric:** Tabular, monospaced values for endpoints and comparable operational data.

**The Instrument Readout Rule.** Use mono only for values an operator compares or transcribes.

## Layout

The default view has three horizontal bands: command rail, five-port patch rail, and the workbench.
The workbench gives the regional route table roughly two-thirds of the width and the selected-route
inspector the remainder. The detail bay below is a single full-width table or timeline, not a grid of
cards. Main surfaces are bounded at 1480px.

At 1120px, secondary table columns collapse and the workbench tightens. At 800px, the workbench
stacks while the patch rail and wide tables become deliberate local scroll areas. At 520px, command
copy and nonessential columns reduce further, but the document itself must remain free of horizontal
scroll at widths down to 360px. Spacing follows a compact 4/8/12px rhythm, with 24-28px reserved for
surface edges and major section separation.

**The Local Overflow Rule.** A rail or data table may scroll horizontally; the page root may not.

## Elevation & Depth

The workbench is flat by default. Dividers, muted surface changes, and the graphite rail establish
hierarchy. A low ambient shadow identifies the selected port, button hover, or segmented selection;
the strongest shadow belongs only to the confirmation dialog.

### Shadow Vocabulary

- **Selected Instrument:** A low, short shadow under the selected socket or pressed segment.
- **Command Hover:** A warm low shadow under an available primary action.
- **Modal Lift:** A broad dark shadow for the blocking switch confirmation dialog.

**The Flat Workbench Rule.** Resting page sections use borders and tonal layering, not floating cards.

## Shapes

Controls are compact and nearly square. Protocol labels use the tightest corners, socket and slot
parts use small corners, ordinary controls use a 6px radius, and only dialogs or fatal-state panels
reach 8px. Circular geometry is limited to status lamps and count pills where the shape carries a
clear instrument meaning.

## Components

### Buttons

- **Primary:** Oxide orange with white icon and text; used for tests, confirmed switches, retries,
  and other direct commands.
- **Secondary:** Paper surface with a strong hairline border; used for reconnect and cancellation.
- **Icon:** A stable 34px square with a visible tooltip or accessible label.
- **Hover / Focus:** Hover changes color without moving layout; keyboard focus uses the blue outline.
- **Disabled:** Remains visible at reduced opacity and uses a not-allowed cursor.

### Inputs / Fields

Inputs sit in a paper-white 46px field with a strong divider border. Focus changes the border to blue
and adds a restrained outer ring. Error copy appears immediately below in failure red.

### Navigation

The command rail owns global actions. The numbered socket rail selects a region. Detail navigation
uses compact tabs with an orange underline and an integrated neutral count pill.

### Regional Port Socket

Each socket combines a status lamp, fixed port number, and region name. The socket position and port
never move when its exit changes; selection uses an orange border and a low shadow.

### Route Trace

The trace reads left-to-right from SOCKS port through the A/B switch to the exit. The outbound wire
becomes teal only when the selected region is healthy; missing data is shown as `--`, never inferred.

### Status And Jobs

Status always combines color with words or icons. Running jobs expose progress, and queued or running
timeline rows expose a cancellable action. Errors retain their exact backend reason.

## Do's and Don'ts

### Do:

- **Do** keep fixed ports and dynamic exits visually distinct.
- **Do** use tabular numeric treatment for ports, IP addresses, latency, and timestamps.
- **Do** show loading, empty, disabled, failed, and reconnecting states in place.
- **Do** keep actions reversible, named, and adjacent to the state they affect.
- **Do** preserve keyboard focus, text labels, and reduced-motion behavior.

### Don't:

- **Don't** replace the workbench with detached metric cards or a marketing dashboard.
- **Don't** use healthy teal for unverified VPN Gate metadata or an API-only score.
- **Don't** hide failure reasons behind a generic offline label when exact evidence exists.
- **Don't** expose desktop-only interactions or allow the document root to overflow on mobile.
- **Don't** add gradients, decorative illustrations, oversized headings, or soft pill controls.
