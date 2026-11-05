# Chill design and verification notes

Screen question: where will the runner go next, and which action is safe now?

The product objects are a saved GCJ-02 track, WGS-84 simulation position, draft
guide points, numbered required checkpoints, the actual state machine, distance,
pace, stride estimate, elapsed time, lap count, device mode and time verification.
All live values come from the engine. Empty values remain dashes and real zeros
remain zeros. Test routes used during browser QA were written only to a temporary
preview configuration, never bundled as a user's default route.

Design direction: `true-black-flat`, seed `733438361`, from the app-screen draw.
The user's explicit specification overrides its opaque surfaces and T-frame:
use a full satellite map, floating 20 px glass surfaces, 28 px blur at 190%
saturation, and the required Apple system font stack. No direction was redrawn.
Identity comes from Chill's brown poodle portrait and the counterclockwise moving route.
Blue is the loop and current location, gold is the draft/branch, green is the
checkpoint zone and completed target. The primary action uses a white fill.

Desktop layout uses 24 px outer gutters, a 300 px editor and a 320 px session
panel, with the entire remaining area reserved for the map. The 768 px preview
places the map above two columns; at 390 px the panels stack below it. The native
desktop window itself has a 980 × 680 minimum.

The skill linter returned no failures. Allowed warnings: glass and overlay shadows
are explicit user requirements; zero padding labels real numbered route nodes;
dashes represent genuinely missing measurements. The Chrome-specific inspector
could not run because no Chrome executable is installed. The available in-app
browser was used instead for full screenshots and bounds checks at three sizes.
Its checks covered track drawing, real marker dragging, smooth regeneration,
language switching, all six checkboxes, the timed hold, cruising, checkpoint
marking, draft confirmation and three-confirmation Sandbox restoration.

The worst three visual issues found and fixed were clipped desktop coordinates,
an incorrect primary-button background overriding its text contrast, and map
occlusion on tablet widths. The bundled Leaflet fallback was added after the
specified CDN script returned HTTP 404. The native canvas path was separately
exercised before installing the local fallback.

The final audit also verified that one or two unfinished anchors do not trigger
invalid-track autosave errors, and that satellite overzoom reuses native level-18
imagery rather than requesting unavailable higher-resolution tiles. Windows
firewall checks use verbose output and the OEM console code page, and refresh
an existing program rule rather than creating duplicates on subsequent starts.

## Stage 4: interactive spotlight tutorial

The existing direction (`true-black-flat`, seed `733438361`) and Apple font and
glass overrides are retained. No new direction or external visual asset was drawn.
The teaching screen answers one question: which control should I use next?
Several independent SVG cutouts focus the relevant controls, with a floating
20 px glass card. Language and replay controls remain available throughout.

The module is UI only. It uses the loaded default-track configuration to decide
whether to open automatically and never sends a motion or device command. It
supports direct map drawing, anchor dragging, template saving, previous/next,
completion, dismissal, Escape, Tab navigation, and reduced-motion preferences.
Existing safety dialogs take priority. Detour drafts survive replay and exit.
The six original preflight checks remain in order, including the clock at index
four; weather and campus exercise hours are a seventh mandatory backend check.
Settlement repeats the phone-submit, app-close, restore, disconnect sequence.
The copy makes no guarantee about third-party account outcomes or trace removal.

The three layout refinements were separate cutouts instead of one large exposed
rectangle, keeping the teaching card stable when search results close, and adding
temporary scroll room so the narrow-screen sheet cannot cover a start or restore
button. This scroll room disappears when the tutorial closes.

Real in-app browser checks covered all five steps in both languages at
1380 x 860, 768 x 1024, and 390 x 844: 30 combinations, no horizontal overflow,
cards inside the viewport, and no overlap with the primary highlighted control.
The Chrome-only inspector remains unavailable on this Mac; screenshots and DOM
bounds were checked in the available browser instead. The saved screenshots are
in `artifacts/piggy-tutorial-*.jpg`, with bounds evidence in
`artifacts/tutorial-browser-layout-checks.json`.

The linter has zero failures. The explicitly requested sparkle and weather emoji
are annotated exceptions. Its remaining 13 warnings concern the requested glass,
floating shadows, existing numbered anchors/time values, and missing-value dashes.
Visual critique: the map remains the working surface, the current action stays
visible, the card has one primary action, and the small-screen footer remains
accessible while longer instructions scroll. All demo data lives in temporary
Sandbox configurations; no sample track is installed as a user's default.

## Stage 5: multi-lap data

The existing direction and glass overrides remain. A compact lap summary shows
the current lap and the actual previous duration and pace; the completed-lap
metric is retained. Historical motion is a single thin pale-blue Canvas line,
above the template and below markers. Its smoothing is disabled to retain small
lane changes. The fallback Canvas caches projected history for its current zoom.
Both language dictionaries include every new metric and history error message.

The history is bounded to 3600 points in both engine and UI. A sequence/session
delta feed avoids transmitting thousands of points at every 10 Hz status poll,
and ignores delayed responses belonging to a previous session. It also recovers
history after a minimized window or a reload. Demo traces are accelerated Sandbox
engine output, not phone-recorded exercise. No demo route is shipped as a default.

## Satellite alignment correction

The previous temporary demo was an illustrative oval: its 31 m bends and tilted
axis were never calibrated to the satellite running strip. It has been replaced
by a reproducible, explicitly GCJ-02 public fixture with an approximately 44.4 m
bend radius, 82.1 m straights and a north-facing axis. The engine still operates
only in WGS-84, and each map boundary performs one conversion.

The editor now exposes usable total track width, 1–8 m, with a bilingual note
that the baseline must be placed midway across the red strip. It is locked with
the other track settings during a session. Existing panels, type and glass
overrides remain. Width input is fixed at 76 px so its label stays readable.
Desktop 1380×1000, tablet 768×1024 and phone 390×844 were checked in both languages;
no horizontal overflow, one history Canvas, no JavaScript console errors.

The final desktop screenshot shows the complete eight-lap bundle within the red
surface, with the north and south bends and both straights visible. This is a
visual fit to satellite imagery, not a claim of surveyed geographic accuracy.
Screenshot evidence and the geometric width/CRS report are under `artifacts/`.
The static design lint reports 0 failures; existing glass, shadow, numeric padding
and missing-value warnings follow the original requested design. The Chrome
inspector remains unavailable; real in-app browser screenshots were inspected.
