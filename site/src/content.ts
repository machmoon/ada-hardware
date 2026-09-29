// Every string, number and link on the page, each with the file that backs it.
// The rule (site/README.md): a number appears here only if the repository can
// back it. Change the page when the measurement changes, not before.
// Mapping from the old page: ada-site-ref/spec.md section 2b.

const REPO = "https://github.com/machmoon/ada-hardware"; // the public repo, README "Links"

/** Contact address the old page already used (site/index.html before the rebuild). */
export const EMAIL = "liujpatrick@gmail.com";
export const DEMO_MAILTO = `mailto:${EMAIL}?subject=Ada%20demo`;
export const demoMailtoWithBody = (text: string) =>
  `${DEMO_MAILTO}&body=${encodeURIComponent(text)}`;

export const LINKS = {
  repo: REPO,
  install: `${REPO}/blob/main/docs/install.md`, // docs/install.md exists
  boardEval: `${REPO}/blob/main/scripts/board_eval.py`, // the evaluation script
  licence: `${REPO}/blob/main/LICENSE`, // MIT
  aceternity: "https://ui.aceternity.com",
  skiper: "https://skiper-ui.com",
} as const;

// Band 1: header. In-page anchors except Install.
export const NAV = [
  { name: "Checks", link: "#checks" },
  { name: "See it work", link: "#see-it-work" },
  { name: "Try it", link: "#try" },
  { name: "Install", link: LINKS.install },
] as const;

// Band 2. Backing: the old page's "run the open-source engine today"; LICENSE is MIT.
export const ANNOUNCEMENT = { text: "Ada's engine is open source. Run it today", href: "#try" };

// Band 3: hero. h1 was the old og:description; the subtitle is the old hero's first sentence, corrected: ERC runs on
// the .kicad_sch and DRC on the .kicad_pcb (engine/silkscreen/verify/kicad.py), while the case is gated by the
// enclosure kernel's signed-margin clauses against the board (engine/silkscreen/enclosure/kernel.py).
// Pat 2026-09-29: lead with the one-line pitch, "the Cursor of hardware engineering", so a visitor knows what Ada is
// before the details. The previous title was "Order boards that work."
export const HERO = {
  title: "The Cursor of hardware engineering.",
  subtitle:
    "Cursor writes code in your editor. Ada designs circuit boards in KiCad: describe a board, get a schematic, a placed and routed board and a printable case, checked by KiCad's own ERC and DRC before you order.",
  // The desktop app's EXAMPLE_PROMPTS, verbatim: app/src/pages/kaleo/components/PromptBar.tsx:109-113
  placeholders: [
    "A 3.3 V LDO board off USB-C with a power LED",
    "A 555 timer LED blinker on a 9 V battery",
    "An ESP32 dev board with USB-C and a reset button",
  ],
  chips: [
    { label: "3.3 V LDO", icon: "zap", prompt: 0 },
    { label: "555 blinker", icon: "lightbulb", prompt: 1 },
    { label: "ESP32 dev board", icon: "cpu", prompt: 2 },
  ],
  // The site cannot run Ada; it says so and hands over the real command.
  // `silkscreen "<intent>"` is the generate CLI: onboard.dispatch falls through
  // to it and -o defaults to board.kicad_pcb (engine/silkscreen/cli.py:429).
  runLine: "Ada runs on your machine, beside KiCad, with your own model key. Run this:",
} as const;

/** POSIX single-quote the intent: each ' becomes '\'' . */
export const shellQuote = (text: string) => `'${text.replace(/'/g, `'\\''`)}'`;

// Band M1. Old page, #what-ada-does header. The floating ground is the
// engine/tests/test_verify.py case (an unwired ground pin is a blocker).
export const REVEAL = {
  title: "A bad board costs weeks. Ada catches it first.",
  body: "It finds the floating ground and the missing pull-up while the design is still a file, not after the boards show up.",
  imageAlt: "Top copper of the ESP32 dev board Ada placed and routed, rendered by kicad-cli",
};

// Band M2. docs/measurements/board-eval-2026-09-16.json: all six cases show
// erc_errors 0, drc_errors 0, unconnected 0, parity 0 (fixed circuits answered
// by a scripted model, per the scripts/board_eval.py docstring, so "test boards").
// 0.25 mm: DIFF_PAIR_GAP_NM = mm(0.25), engine/silkscreen/diffpair.py:66; engine/tests/test_diffpair.py.
export const CHECKS = {
  title: "6 of 6 test boards pass KiCad's own checks.",
  body: "Zero ERC errors, zero DRC violations, and a schematic that matches the board on every one. USB routes as a coupled pair at a fixed 0.25 mm gap.",
  caption:
    "Ada's board evaluation, Sep 16, 2026. Built on KiCad, ngspice, OR-Tools, OpenCascade, build123d and FreeCAD.",
  link: { text: "See how we measure it", href: LINKS.boardEval },
  drawingLabel: "Two traces routed at a constant gap",
};

// Band M3.
export const SEE = {
  title: "See it work",
  body: "What Ada designs, how it checks it, and what it hands you.",
  receipt: {
    title: "One receipt per board",
    body: "Every claim about the board names the check that proved it. One failed check stops the run.",
    // Counts follow the rows shown. The old page's SPICE and CASE rows and its
    // per-check timings are held: nothing in the repo backs those values (spec 2b).
    tabs: [
      { label: "Passed", count: 3, active: true },
      { label: "Warnings", count: 0, active: false },
      { label: "Blocked", count: 0, active: false },
    ],
    // CLAUDE.md "Verifying against real KiCad", measured on KiCad 10.0.6; board-eval JSON.
    rows: [
      { id: "ERC", text: "KiCad 10.0.6, 0 errors" },
      { id: "DRC", text: "0 violations, 0 unconnected" },
      { id: "PARITY", text: "Schematic and board agree" },
    ],
  },
  kicad: {
    title: "Works in your KiCad",
    // Schematic, placement and routing open in KiCad: service/steps.py:185-189 _BRIDGE_STAGE (propose -> schematic,
    // place -> placement, route -> routing) through desktop/kicad_live.py. "Never places" the order: service/steps.py:16 "prepare
    // (never submit) a fab order" and :47 "Nothing here submits an order". The old page's "nothing that costs money
    // runs until you approve it" was wrong: place starts the case and the BOM on model calls nobody pressed for
    // (service/steps.py:1246-1247, _prefetch_case and _prefetch_sourcing).
    body: "Ada drives the KiCad you already have. The schematic, placement and routing each open there for you to check, and Ada prepares the fab order but never places it.",
    imageAlt: "Ada's strip: prompt field, microphone, attach, history, settings, Generate",
  },
  fixed: {
    title: "Fixed before you see it",
    body: "KiCad's ERC runs while Ada draws. A floating ground pin in round one is fixed by round two.",
    // engine/tests/test_verify.py:88-93, 194-200; engine/tests/test_harness.py:80-95
    log: [
      { kind: "dim", text: "propose · round 1" },
      { kind: "bad", text: "AMS1117-3.3.GND is on no net" },
      { kind: "plain", text: "  put it on the ground net" },
      { kind: "bad", text: "erc.pin_not_connected  U1 pin 1" },
      { kind: "plain", text: "" },
      { kind: "dim", text: "propose · round 2" },
      { kind: "good", text: "one ground: GND" },
      { kind: "good", text: "erc clean" },
    ],
  },
  routed: {
    title: "Routed the way you would",
    body: "Ground poured on both layers and stitched together. USB routed as a real differential pair.", // CLAUDE.md 2026-09-15/16
    imageAlt: "The ESP32 dev board Ada placed and routed, rendered by kicad-cli from the KiCad file",
  },
  review: {
    title: "A second opinion on every board",
    body: "Ada reviews its own design like a skeptical senior engineer. Every finding names the pins and the fix, and anything the netlist can't back gets dropped.",
    // The findings are carried over from the old page, which captioned them "A real review of an ESP32 dev board,
    // Sep 15, 2026". No file in the repo carries them (signals.py has the UART-crossing and I2C-pull-up rules that
    // would raise the first two), so the caption is spec 2b's fallback until someone names the run.
    caption: "An example of a review, on an ESP32 dev board.",
    findings: [
      {
        tab: "U1 · J2 serial header",
        title: "Serial header TX and RX wired straight through",
        body: "TXD0 lands on the header pin named TXD. On an FTDI adapter that pin is an output too, so two drivers fight and both receivers float.",
        fix: "fix: cross the pair, TXD0 to header RXD",
      },
      {
        tab: "U1 · J6 I2C bus",
        title: "No pull-ups on SDA and SCL",
        body: "The Qwiic port hangs off IO21 and IO22 with nothing pulling either line to 3.3 V. I2C is open-drain, so the bus never idles high.",
        fix: "fix: 4.7 kΩ from SDA and SCL to +3V3",
      },
      {
        tab: "U2 · J2 3.3 V rail",
        title: "The header's 3.3 V pin backfeeds the regulator",
        body: "The header shares the regulator's output net with no diode, so a 3.3 V cable and USB-C plugged in together fight each other.",
        fix: "fix: a Schottky or a solder jumper on that pin",
      },
    ],
  },
};

// Band M4. The wheel URL answers 200 on machmoon/Ada releases (not on
// machmoon/ada-hardware); `silkscreen serve` is in onboard.dispatch.
export const TRY = {
  title: "Try it",
  body: "Get the Mac app early, or run the open-source engine today.",
  mac: {
    title: "Ada for Mac",
    body: "Early access for Apple silicon with KiCad 8 or newer. We set up every team by hand.",
  },
  engine: {
    title: "Run the engine yourself",
    body: "Bring your own model key. Ada sends your request and the datasheets to that model, and nothing else.",
    commands: [
      'pip install "silkscreen[agents,cad] @ https://github.com/machmoon/Ada/releases/download/v0.1.0/silkscreen-0.1.0-py3-none-any.whl"',
      "silkscreen serve",
    ],
  },
};

// Band M5. The old page's hero audience sentence and its closing line.
export const CLOSING = {
  title: "See what Ada catches in your next board.",
  body: "For a beginner before the first dead board, and a senior engineer before fab.",
};

// Band 4. The old closing band's h2 is the tagline.
export const FOOTER = {
  tagline: ["Know it works", "before you order."],
  columns: [
    {
      heading: "Ada",
      links: [
        { name: "See it work", href: "#see-it-work" },
        { name: "Checks", href: "#checks" },
        { name: "Try it", href: "#try" },
      ],
    },
    {
      heading: "Open source",
      links: [
        { name: "GitHub", href: LINKS.repo },
        { name: "Install", href: LINKS.install },
        { name: "Board evaluation", href: LINKS.boardEval },
        { name: "MIT licence", href: LINKS.licence },
      ],
    },
    {
      heading: "Contact",
      links: [
        { name: "Get a demo", href: DEMO_MAILTO },
        { name: "Email", href: `mailto:${EMAIL}` },
      ],
    },
  ],
  copyright: "© 2026 Ada",
};
