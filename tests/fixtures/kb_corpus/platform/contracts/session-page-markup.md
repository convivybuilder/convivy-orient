---
id: platform/contracts/session-page-markup
kind: reference
project: platform
title: Contract — the conversation page markup Deck's session pane displays
status: current
topics:
  - contracts
  - deck
  - session-view
audience:
  - team
last_reviewed: '2026-08-09'
authors:
  - operator
---

# Contract — the conversation page markup

**Deck reads this page's structure, not only its text, and until now nothing
recorded that.** The session pane runs host JavaScript against the rendered
document to preserve what the reader was doing across a reload, so a selector or
an attribute renamed in agent-platform silently stops a Deck feature working
with no error on either side. That is the class of failure this doc exists to
catch, and it is the same one [[platform/contracts/document-render-html]]
catches for the file viewer.

## Producer and consumer

| | |
|---|---|
| Producer | `agent-platform/lib/agent-platform/session_view.py` — `render_html` |
| Rows built by | the same file — `classify`, `rows_for`, `fold_results`, `held_rows_for` |
| Consumer | `convivy-deck`'s `ConversationWebView` and `ConversationScrollState` |
| Transport | `session-view --render` over the ssh ControlMaster, and `--delta-since <n>` for an incremental update |

## The premise: the document is replaced whole, every time

Deck calls `loadHTMLString` with a freshly rendered page whenever the
conversation changes. Nothing in the old document survives that, so anything the
reader established by interacting with the page has to be read out before the
load and put back after. Two things are read out today, and both need a stable
handle in the markup.

**The append bridge removes the need for all of this, and it is being built
rather than anticipated.** [[deck/specs/session-pane-incremental-rendering]] is
the design and agent-platform #377 is the producer half. The handles below stay
load-bearing under it, and matter more rather than less: an appended row has to
be told apart from one already on the page, and a row already on the page has to
be findable before it can be replaced.

## `.turns` is the scroller, scrollTop 0 is the bottom, and history is NEGATIVE

The turns live in one `<div class=turns>` and are emitted in reverse inside a
`flex-direction: column-reverse` container. The flow therefore starts at the
newest turn, which is where a fresh document opens, and the visual order still
reads oldest-first because the reversal cancels out.

**Scrolling up into history produces NEGATIVE `scrollTop` in WebKit, and saying
only that zero is the bottom is what made this doc insufficient.** Measured
against a real rendered page on 2026-08-08, in a 700×150 `WKWebView`:

```
scrollHeight = 413   clientHeight = 93    at rest: scrollTop = 0
scrollTop = 60   ->  clamps back to 0     (positive is unreachable)
scrollTop = -60  ->  stays -60            (history)
```

A consumer that assumes history runs positive writes an at-bottom test as
`scrollTop <= tolerance`, which every reachable value satisfies, so the test
returns true forever. Deck shipped exactly that, and two features were dead
without a single failing test: the jump-to-new-output pill could never rise,
since it is cleared whenever the reader reads as at-bottom, and #71's scroll
restoration always stored `nil` and put nothing back. Both were fixed in
convivy-deck #83 by comparing the MAGNITUDE, which is the question actually
being asked and cannot be inverted by a sign convention.

**Content added at the bottom moves a scrolled-up reader, and the consumer has
to compensate.** The turns are emitted REVERSED, so the DOM's first child is the
NEWEST turn and its last child is the oldest, and a new turn therefore arrives at
the DOM start, which is the visual bottom. The document grows underneath the
reader while `scrollTop` keeps its old value, sliding the viewport toward the
newer content by exactly the height that arrived. Deck subtracts that growth from
`scrollTop` when the reader is up in history, and deliberately does NOT when they
are at the newest turn, where holding position would scroll them off the turn
that just landed.

**Which end grows is worth stating, because compensating for the wrong end is
backwards rather than merely useless.** Growth at the oldest end does not change
the reader's distance from the bottom and needs no correction at all; a test that
simulated growth by appending to the DOM measured the correction moving the
reader 50 px the wrong way, and was itself the thing that was wrong.

**BOTH of Deck's update paths carry the correction, and for one release only one
did.** The delta apply compensates in `applyDeltaBody`. The reload path
(`ConversationWebView` reads `.turns`'s `scrollTop` before a reload and restores
it after) needs it just as much, and arguably more: the one thing that reliably
causes a reload is a delta that could not be applied, which fires precisely
because new content arrived. So the replacement document is taller than the one
measured, and reapplying the raw offset lands the reader off by the difference.
`readScript` therefore carries `scrollHeight` alongside `scrollTop`, and
`restoreScript` spends it. Both landed in convivy-deck #83.

Change the container to normal column order and every one of the behaviours
above inverts.

## `data-k` identifies a row across a reload

Every `<details>` **and** every `<div class='turn …'>` carries `data-k="<key>"`.
The collapsed rows got it in agent-platform #372; the full rows — what a human
typed, what the model said — got it in #377, because the append bridge has to
address those and could not.

Deck reads the keys of the open rows before a reload and re-opens the same keys
after, as of convivy-deck #79, so a tool call the reader expanded stays expanded
while the session keeps producing output. That is the one thing the pane exists
to let them do, and until that pair of changes every new line closed it. The
append bridge reads every row's key instead, to replace a row in place rather
than reload the page around it.

**The key must identify the row, never its position.** An index-based key looks
correct until a turn is inserted above one, at which point the same index is a
different row and the reader finds an unrelated call expanded. That trades a
visible defect for a silent one, which is worse.

```
<uuid>.<block-index>     a row from a transcript record that has a uuid
h<16 hex chars>          fallback: a hash of the row's kind, label and body
<key>~<n>                disambiguator, appended when a key would repeat
```

The transcript record's `uuid` is the identity, because a record is written once
and appended to a file, so its uuid is fixed for the life of the transcript, and
one record's blocks are told apart by their index within it. Measured against a
real 72-line transcript on 2026-08-05: every **visible** row keyed by uuid, and
the nine hash-fallback rows were all `HIDDEN` control records that never reach
the page.

**A key's charset is an allowlist, and it is load-bearing security.** The key is
interpolated into an HTML attribute here and into host-evaluated JavaScript in
Deck, and the transcript is a file Anthropic's tooling writes and tool output
flows through. Deck's restore script wraps each key in a double-quoted
JavaScript string with no encoder behind it, so the charset is the only thing
keeping a key inside its own literal.

**The emitted key's charset is `[A-Za-z0-9._~-]`, which is wider than the
`uuid`'s.** The producer validates `uuid` against `[A-Za-z0-9._-]{1,100}` and
falls back to the hash for anything outside it, then builds the key by adding a
`.` before the block index and, on a repeat, a `~n` suffix. So a consumer
allowlisting only the uuid's characters would drop every disambiguated key,
which is a real gap rather than a pedantic one. Deck's `rowKeyPattern` is
`^[A-Za-z0-9._~-]{1,120}$` for that reason. `~` is not special in HTML, in a
JavaScript string, or in a quoted CSS attribute selector, so widening to it
costs nothing.

`data-k` is HTML-escaped on the way out as well, and Deck validates rather than
escapes on the way back in, since the key returns inside a script the host
evaluates.

## A held row draws a queued message that has not been read yet

**Input-robustness Slice D** (agent-platform, `session_view.py`'s `held_rows_for`)
folds a session's `queue-operation` records — the same fold
[[platform/contracts/session-agent-api]]'s `held` state field uses — and emits
each currently-queued message as a turn row in the SAME markup a read `you`
turn uses, plus one extra class:

```
<div class='turn you held' data-k="~held.<index>.<16 hex chars>">
  <div class=who>you (queued)</div>
  <div class=body>…</div>
</div>
```

A native SwiftUI overlay was tried first and rejected: measured against
`session-view`'s real typography off-screen, line spacing was wrong in 6 of 6
scale/appearance combinations tested, because CSS distributes `line-height` as
half-leading while `.lineSpacing()` adds a flat amount below each line — two
different box models, not a constant worth chasing. Rendering the held message
as a real turn in the document gets the typography by construction instead.

**A held row is not in the transcript, and it disappears on release.** It has
no transcript record, no `uuid`, and no life beyond one render: the next time
the page is generated after the message is read, `fold_queue` no longer
reports it, and the row that was standing in for it is simply absent — replaced
by the ordinary `you` turn the released message becomes, which is a different
element with a different key. Nothing here makes them the same element across
that transition, and nothing tries to. On a full render the old held row is
just missing from the freshly built page; on a delta — below — its key is
named in `removed`, because the append bridge is not rebuilding the page and
has to be told which element to take out of it.

**A consumer must treat an unknown class on a turn as cosmetic.** `held` is
additive to `you`, never a substitute for it — a client that does not know the
class renders the row exactly like an ordinary `you` turn, and only a client
that recognises `held` dims it. This is what let the class ship without a
Deck-side change in the same PR: an old Deck build already displays a held row
correctly, just without the visual distinction.

**The key scheme is `~held.<index>.<digest>`, and the leading `~` is
load-bearing.** `_row_key` (the scheme above, "`data-k` identifies a row
across a reload") only ever emits a string that STARTS with either a `uuid`
that passed its own charset check (`[A-Za-z0-9._-]`, no `~`) or the literal
`h` fallback prefix, and appends `~n` only as a SUFFIX to one of those two. So
no real row key can ever start with `~`, which makes a leading `~` collision-
proof independent of transcript content — even a `uuid` field engineered to
read like a held key falls back to the hash instead of being used verbatim,
because `~` is outside the charset that field is checked against.

**This is where that guarantee lives**, and
[[deck/specs/session-pane-incremental-rendering]] leans on it: its argument that a
removal key can never match several rows rests on transcript keys being unique by
the check below and held keys being unique against them by this prefix. A change
to the `~` here breaks that argument one repo over.

`index` is the message's position in the current fold and `digest` is a short
hash of its own text.

**So a held row's key is NOT stable, and a consumer must not assume it is.** It
holds only while the message keeps its position, and a release takes the position
away from every message behind the released one: the queue is compacted, indices
are recomputed by `enumerate` on the next fold, and each survivor comes back under
a key it has never had before. Releasing the first of two queued messages
withdraws BOTH keys and draws ONE row. That is the shape
[[deck/specs/session-pane-incremental-rendering]] documents on the delta side,
and it is why `rows` resends the whole held set rather than a delta over it.

**Release is one `remove` per message, not a batch**, which is the correction
[[agent-platform/contracts/claude-transcript-jsonl]] landed on 2026-08-10 — an
earlier reading of "the queue drains as a batch" came from two `dequeue` records
sharing a millisecond, and every mid-turn release measured on 2.1.226 uses
`remove`, one record per message. The consequence here is that the queue is
briefly one shorter rather than empty, so a fold between two releases has
survivors to re-key rather than nothing to draw.

The charset stays inside `[A-Za-z0-9._~-]`, the emitted-key allowlist this doc
already documents, so Deck's `rowKeyPattern` needs no change.

**No new colour.** `.you.held .body` narrows the read treatment's 3px solid
`currentColor` rule to a 1px dashed one and lowers `opacity` from .95 to .55 —
both still `currentColor`. Presence, position and the `.who` label ("you
(queued)") carry the distinction; a new hue would add nothing to something the
reader typed two seconds ago at the spot their eye already is.

**A non-user enqueue is filtered before it reaches the page.** The queue
carries more than typed messages — a `<task-notification>` blob from a
background task appears in the corpus — and `held_rows_for` filters these with
the same check `classify` already runs on ordinary `user` records, for the
same reason: the harness writes into this stream in the user's voice too, and
drawing its text as "your held message" would misattribute it.

**That filter is PARTIAL, and a consumer should not read it as a guarantee.** It
matches four literal prefixes, and measured against the local corpus's 21,133
enqueue records carrying string content it caught 12,452 of them — **58.9%**. The
41.1% it does not match are dominated by agentctl dispatch prompts, which are
machine text and will render as a held turn labelled as the reader's own. Tracked
as `issue_01KZN2N7R6EZN75X459H4DYVKF`. The check is worth having and is not the
whole answer; a second, content-shaped guard sits beside it in `session_view` for
the same reason, and it is partial too.

## The delta path draws held rows too, and can name one to remove

`render_delta` is the path Deck's `SessionPaneModel` actually calls once a
cursor already exists — which is whenever a reader has the pane open, so it is
the steady state, not the exception. Scoping held rows to full render only, as
originally shipped, left the feature invisible in exactly that case: a queued
message advances the transcript's line count without producing a visible row
of its own, so a plain "rows after cursor" delta had nothing to draw. Now it
emits the session's currently-held rows in `rows`, the same markup as above,
appended after the transcript-line-derived ones, and grows a `removed` field:

```json
{
  "lines": 412,
  "full": false,
  "rows": [{"key": "…", "html": "…"}],
  "removed": ["~held.0.<digest>"]
}
```

`removed` is always present, `[]` when there is nothing to remove. It names
held-row keys the client is currently showing that are no longer held;
[[deck/specs/session-pane-incremental-rendering]]'s append bridge deletes them
by the same `data-k` lookup it already uses to find a replacement target.

**A held row is transient and can leave the page with no transcript growth a
consumer can point to.** Every other key `removed` will ever name corresponds
to something that changed in the transcript; a held row does not — `dequeue`,
`popAll` and `remove` queue-operation records are themselves transcript
lines, so the line count still advances, but they classify HIDDEN and produce
no row of their own. What actually leaves the page is a synthetic row keyed
off queue position, not a transcript record. A consumer that expects `removed`
to only ever name a row backed by a line it can see will be surprised by this
one.

**The removal list is reconstructed, not guessed, and here is how it knows.**
`cursor` is a count of transcript lines and says nothing on its own about
which held keys are currently on the client's page — a queue can drain
without the line count changing in a way that produces a row. But the
transcript is append-only JSONL, `held_rows_for` is a pure function of the
lines it is given, and every caller of `render_delta` — full render included —
stamps `cursor` as the exact line count it read to build the response the
client is holding. So `raw_lines[:cursor]` is not an approximation of what the
client last saw; it IS the file the earlier call read. Folding that identical
prefix reconstructs the client's current held rows exactly, and whatever key
is present there but absent from the fold over the whole file now is what
`removed` names.

**THE ORDERING CONSTRAINT.** This ships only after Deck's append bridge can
remove a row by key, not before. Landed first, a held row would appear via
delta and never leave: the bridge could only replace or insert a row, so
`removed` would name a key nothing on the consumer side acts on, and a released
or deleted message would sit on the page drawn as "waiting" forever — worse
than the pre-Slice-D behaviour of showing nothing at all.

## Compatibility rules

1. **Adding an element, a class or an attribute is not breaking.** Deck displays
   what it is given.
2. **Renaming `.turns`, or dropping `column-reverse`, IS breaking**, and it
   breaks scroll preservation, the at-bottom predicate, and the insert-time
   scroll compensation together. Dropping `column-reverse` is the worst of the
   three to do quietly: the sign of `scrollTop` flips, so the consumer keeps
   running and simply draws the wrong conclusion from every reading.
3. **Renaming `data-k`, or dropping it from any row, IS breaking**, and the two
   kinds of row fail differently. An unkeyed collapsed row degrades to closing
   on every reload. An unkeyed full row is invisible to the append bridge, so a
   message never reaches the page at all until the next full render — silent,
   and worse than the row that merely closes.
4. **Widening the emitted key's charset past `[A-Za-z0-9._~-]` IS breaking**,
   because Deck validates against that allowlist before it interpolates a key
   into script, and drops anything outside it. A key Deck drops is a row that
   does not reopen.

   **Updating both sides in step removes the breakage and NOT the danger, so
   this rule constrains what you may add, not only where.** Deck builds a
   JavaScript array literal by wrapping each key in double quotes, with the
   charset standing in for an encoder. A character that is special inside a JS
   string — `"`, `\`, a line terminator including U+2028 and U+2029 — turns
   that literal into script the transcript chose, executing through
   `evaluateJavaScript`, which `allowsContentJavaScript = false` does not stop.
   So a widening may add only characters that are inert in a double-quoted JS
   string, in an HTML attribute value, and in a quoted CSS attribute selector.
   To add a character that is not, replace the hand-built literal with a real
   JSON encoder in the same change; validating harder is not a substitute,
   because the next widening faces the same question again.
5. **Changing a key's derivation is breaking-in-one-frame.** Every open row
   closes once, then behaves. That is acceptable for a derivation change and
   worth saying out loud rather than discovering.
6. **The `deck-open:` hrefs in a collapsed row's summary follow
   [[platform/contracts/document-render-html]]**, which owns that shape. This
   page emits them through `_summary_label`; it does not define them.

## Executable check

- `agent-platform/tests/test_session_view.py` pins that every row carries a key,
  that a key does not move when a turn is appended, that folding a result keeps
  the call's key, that keys stay unique when a uuid repeats, and that a hostile
  uuid falls back to the hash. It also pins the delta path: a newly-queued
  message appears in `rows`, a drained one is named in `removed`, both happen in
  the same response when a release and a reply land in the same poll, and a
  client cursor from before an entire enqueue-then-drain cycle still converges
  on the correct current state rather than fabricating a removal or leaking a
  phantom held row it was never shown.
- `convivy-deck`'s `OpenRowPreservationTests` pins the key allowlist and what it
  refuses, the read parse under every wrong shape the page could return, and the
  restore script's ordering, plus a round trip against a real `WKWebView` with
  `allowsContentJavaScript = false`.
- `convivy-deck`'s `DeltaScrollAnchoringTests` pins the scroll behaviour above
  against a real column-reverse container: a reader up in history keeps the same
  row in the same place on screen when a turn arrives, a reader at the bottom
  stays pinned, and the at-bottom predicate treats negative values as history.
  It asserts an anchor's `getBoundingClientRect().top` rather than arithmetic on
  `scrollTop`, because an arithmetic assertion can agree with a wrong sign
  convention while the reader still moves.

**Nothing asserts across the seam.** As with the file viewer's contract, each
side tests its own half against its own expectation, so a renamed attribute
passes both suites and fails in the app. A check that rendered a fixture
transcript with `session-view` and asserted Deck's parser finds its keys would
close it, and it belongs wherever both sides are reachable, which today is
nowhere.

## Change protocol

1. A change to any handle above updates this doc in the same PR.
2. agent-platform reaches the mini on a fetch with no deploy step, so a change
   that alters a handle lands with the Deck change **ahead** of it, not behind.
   The mini's `~/.local` symlinks point into the primary checkout, so a merged
   change does nothing until that tree pulls.
3. A review of either repo checks this doc moved with the change. That is
   convivy-deck's standing rule for every contract surface.

## Related

- [[platform/contracts/document-render-html]] — the sibling contract for the
  file viewer, and the format this follows.
- [[platform/contracts/session-agent-api]] — the state Deck's session pane draws
  its native chrome from.
- [[deck/specs/session-pane]] — the surface this serves.
- [[deck/specs/session-pane-incremental-rendering]] — the append bridge, which
  supersedes read-and-restore in the common case and keeps it as the fallback.
