# Prior art: incremental conversation-state tracking for Commitment extraction

Research for issue #22 (map #20, v1.0 redesign). Researched 2026-11-05.

**Question.** How do existing systems and the literature track plans across a long-running
conversation *incrementally*, instead of re-extracting from a sliding window on every poll?
What are the design options and their known failure modes (hallucination, missed plans,
wrong attribution, long-range date anchoring)?

**Current design (v0.14), for contrast.** Each poll replays the last ~30 messages plus older
date-bearing "anchor" messages, re-detects plans from scratch, then pushes each detection
through a gate stack and a three-layer reconciliation (hash, fuzzy, LLM adjudicator) against
`state.json` (README "How it works"). The model is stateless; the "state" is reconstructed
downstream by matching. Most of the gate/reconcile machinery exists to undo the side effects
of re-extracting the same plan many times.

---

## 1. Dialogue state tracking (DST): state as memory, updated by operations

**What it is.** Task-oriented dialogue systems keep a *belief state* (slot → value) and update
it each turn. The design question we face — "re-predict everything from history" vs "update
a carried state" — is the central efficiency/accuracy axis in this literature.

- **SOM-DST** (Kim et al., ACL 2020) states the problem directly: earlier models "predict the
  dialogue state at every turn from scratch", which is inefficient. SOM-DST instead treats the
  state as "an explicit fixed-sized memory" and, per slot, predicts an **operation**
  (CARRYOVER / DELETE / DONTCARE / UPDATE), generating values only for slots being updated.
  Input is only the previous turn, the current turn, and the previous state — the paper argues
  the previous state "can serve as a compact representation of the whole dialogue history."
  [arXiv 1911.03906 / ACL 2020](https://ar5iv.labs.arxiv.org/html/1911.03906)
- **Key failure mode: error propagation.** Because inference feeds back the model's *own*
  predicted state, mistakes persist. SOM-DST reports joint accuracy 53.01 with predicted
  previous state vs 81.00 with ground-truth previous state — error propagation "increases the
  error 2.47 times." Operation prediction (not value generation) accounts for ~91–93% of the
  remaining error. [same source, Table 4]
- **LLMs as zero/few-shot state trackers** work but trail specialized models on explicit
  belief-state tracking (Hudeček & Dušek, SIGDIAL 2023). [arXiv 2304.06556](https://arxiv.org/abs/2304.06556)
  IC-DST (Hu et al., 2022) does in-context DST with retrieved exemplars and outperforms
  fine-tuned few-shot models. [arXiv 2203.08568](https://arxiv.org/abs/2203.08568)

**Relevance.** A per-thread ledger of Commitments is a belief state; the LLM call becomes
"given the ledger and the new messages, which operations apply?" rather than "find all plans
in these 30 messages." This removes re-detection of settled plans (the source of v0.14's
duplicates/flaps) by construction — but trades it for sticky errors.

## 2. Action items / commitments in meetings and email: decompose the commitment

- **Action items in multi-party dialogue** (Purver, Ehlen & Niekrasz 2006; Purver et al.,
  SIGdial 2007). Treating "action-item-related utterance" as one class was too coarse. They
  annotate utterances by the *role they play* in an action item: **task description,
  timeframe, owner, and agreement**, with one utterance able to fill several roles (e.g. "OK,
  I'll do that" is both *owner* and *agreement*). Inter-annotator agreement was best on
  timeframe (κ = .86) and worst on agreement and description (κ = .73). Detection uses a
  hierarchical classifier over these sub-classes and exploits local dialogue structure;
  a second stage extracts description and timeframe phrases.
  [ACL W06-3405](https://aclanthology.org/W06-3405/), [SIGdial 2007](https://aclanthology.org/2007.sigdial-1.4),
  component list restated in [Microsoft ICASSP'08](https://microsoft.com/en-us/research/wp-content/uploads/2008/01/ICASSP08_1.pdf)
- **Commitment detection in email** (Azarbonyad, Sim & White, Microsoft Research, WSDM 2019):
  commitment classifiers degrade significantly when moved across email corpora; domain
  adaptation is needed. [paper PDF](https://microsoft.com/en-us/research/uploads/prod/2019/01/AzarbonyadWSDM2019.pdf)
- **Smart To-Do** (Mukherjee et al., ACL 2020, Microsoft): two-stage pipeline — detect
  commitment sentences where the *sender* promised an action, then generate the to-do.
  [ACL 2020.acl-main.767](https://aclanthology.org/2020.acl-main.767)

**Relevance.** Attribution failures ("not mine") map onto the *owner* + *agreement* roles:
a Commitment exists for the user only when an agreement act *by the user* (or an explicit
inclusion of the user) is present. Making owner/agreement separate, evidenced fields — each
pointing at a message ID — is the established way to make attribution checkable. Domain
shift (email → iMessage, other people's corpora → owner's) is a documented accuracy risk,
which supports the map's plan to build a labeled corpus from the owner's own `chat.db`.

## 3. Shipping products: Gmail and Siri Suggestions

- **Events from Gmail** adds flights, trains/buses, hotel and restaurant reservations, and
  ticketed/registration events; it does *not* fire when the user is cc'd, the mail went to a
  list, or the message isn't a confirmation of one of those types. Events stay updated when
  the reservation changes. [Google Calendar Help](https://support.google.com/calendar/answer/6084018)
- **Siri Suggestions in Calendar (macOS)** puts events detected in apps like Mail (e.g. a
  flight, restaurant booking, car rental "from known providers") into a separate *Siri
  Suggestions* calendar; the user keeps one by moving it to another calendar or discards it
  with Ignore; unconfirmed suggestions are erased if the feature is turned off.
  [Apple Support](https://support.apple.com/guide/calendar/use-siri-suggestions-iclc121e66ee/mac)

**Relevance.** Neither product attempts free-form plan tracking in casual chat; both restrict
auto-creation to structured, single-message confirmations, and Apple routes the rest to a
suggestion tier requiring a tap. I found no first-party documentation of how either tracks
state across messages. The takeaway is about scope: the two-tier (auto + Suggestion) design
already settled on #20 matches what the big vendors ship, and they treat
casual-conversation plans as too risky to auto-create at all.

## 4. LLM long-horizon memory

- **MemGPT** (Packer et al., 2023): OS-style tiers — a fixed-size, editable *working context*
  in the prompt (writable only via function calls), plus *recall storage* (complete message
  log, searchable) and *archival storage*. [arXiv 2310.08560](https://arxiv.org/abs/2310.08560)
- **Mem0** (2025): incremental extraction from (conversation summary, last m=10 messages, new
  message pair) → candidate facts; each candidate is compared to the top-10 similar stored
  memories and an LLM chooses **ADD / UPDATE / DELETE / NOOP** via tool call. The graph
  variant marks contradicted relations **invalid rather than deleting** them "to enable
  temporal reasoning." On LoCoMo, full-context still scores highest overall (J 72.90 vs 66.88
  for Mem0), at much higher latency/token cost. The OpenAI-memory baseline's poor temporal
  scores are attributed mainly to **missing timestamps in stored memories**.
  [arXiv 2504.19413](https://arxiv.org/html/2504.19413)
- **Zep / Graphiti** (2025): bi-temporal facts — an event timeline (`t_valid`, `t_invalid`:
  when the fact is true) separate from an ingestion timeline (`t_created`, `t_expired`: when
  the system learned/retired it). Contradictions set the old edge's `t_invalid` instead of
  deleting. Relative dates are resolved using each message's reference timestamp as "now".
  Entity extraction uses the current message plus the last 4. [arXiv 2501.13956](https://arxiv.org/html/2501.13956v1)
- **LongMemEval** (Wu et al., ICLR 2025): long-context LLMs drop 30–60% on long chat histories
  vs reading only the relevant evidence (GPT-4o 0.870 → 0.606); commercial ChatGPT memory
  scored 0.577 vs 0.918 offline, and was observed to *overwrite crucial information* as
  conversations continued. "Time-agnostic" memory designs do poorly on temporal reasoning;
  indexing items by event date and filtering by a time range extracted from the query improves
  temporal recall ~7–11%, but only with a strong model (a small model made it worse by
  hallucinating/missing temporal cues). Replacing raw turns with summaries/extracted facts
  usually **hurts** because of information loss; keeping the raw turn and *augmenting* it with
  extracted facts works best. [arXiv 2410.10813](https://arxiv.org/html/2410.10813v2)
- **LoCoMo** (Maharana et al., ACL 2024): ~300-turn, up-to-35-session conversations; LLMs
  struggle with long-range temporal and causal dynamics; long context and RAG help but stay
  well below humans. [arXiv 2402.17753](https://arxiv.org/abs/2402.17753)
- **Lost in the Middle** (Liu et al., TACL 2024): accuracy is highest when relevant information
  is at the start/end of the input and degrades significantly in the middle; performance drops
  as context grows even for long-context models. [arXiv 2307.03172](https://arxiv.org/abs/2307.03172)
- **Anthropic memory tool** (official docs): client-side, application-controlled file store
  the model reads/edits via tool calls; docs recommend capping memory size and expiring stale
  entries, and pairing with compaction for long sessions.
  [platform.claude.com](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)

## 5. Temporal anchoring ("Friday" established weeks earlier)

- **TimeML/TIMEX3**: relative temporal expressions are normalized to ISO 8601 *relative to an
  anchor* (`anchorTimeID` / `temporalFunction`); the default anchor is the document creation
  time, without which "relative time expressions cannot be resolved."
  [Stanford CoreNLP Timex docs](https://nlp.stanford.edu/nlp/javadoc/javanlp/edu/stanford/nlp/time/Timex.html),
  [PMC4986666](https://pmc.ncbi.nlm.nih.gov/articles/PMC4986666)
  In chat, the "document" is each message; its timestamp is the default anchor, and an
  earlier explicit date is an *alternative anchor* that must be linked explicitly.
- **Test of Time** (Fatemi et al., ICLR 2025): benchmark separating temporal semantics from
  temporal arithmetic; LLM performance is sensitive to fact ordering.
  [arXiv 2406.09170](https://arxiv.org/abs/2406.09170) (v0.14's own README independently
  observes Claude's weekday arithmetic is unreliable.)
- Zep and LongMemEval (above) both converge on: store the *resolved absolute date* with the
  fact at ingest time, keyed to the message timestamp, rather than re-resolving raw text later.

**Relevance.** The weeks-later "Friday" problem is an *anchor-linking* problem. Stateless
re-extraction must keep the anchor message in the window (v0.14 harvests anchor messages and
adds a second-pass resolver to compensate). In a ledger design the anchor is already resolved
and stored on the Commitment ("trip: 2026-10-10, evidence msg #123"), so a later "Friday"
message is interpreted against the ledger entry, not against raw text that may have scrolled
away.

---

## Design options and failure modes

| Option | How it works | Known failure modes |
|---|---|---|
| **A. Stateless window re-extraction** (v0.14) | Re-detect all plans from last N messages every poll; reconcile downstream | Same plan re-litigated every message → duplicates and status/date flaps; anchors fall out of window; needs heavy dedup/gates; cost grows with window × polls |
| **B. Full-history long context** | Send whole thread each time | Lost-in-the-middle; 30–60% drop on long histories (LongMemEval); cost/latency; still stateless, so same flapping as A |
| **C. Incremental ledger + operations** (DST/Mem0/Zep pattern) | Per-thread ledger of Commitments is in the prompt; LLM sees ledger + new messages + short recent window and emits CREATE / UPDATE(field) / CANCEL / NOOP against ledger IDs | Error propagation — a wrong entry is sticky and conditions all later updates (2.47× error in SOM-DST); missed operation = silently stale entry; ledger can drift from what was said if raw evidence isn't kept |
| **D. Retrieval memory** (MemGPT/LongMemEval) | Store messages/facts; retrieve relevant ones per new message | Retrieval misses → missed anchors/plans; summaries lose detail; time-agnostic indexing fails temporal questions; extra latency |
| **E. Hybrid C + D** | Ledger for active Commitments; raw log retrievable for evidence/anchors | Most moving parts; but each failure is localizable |

Attribution (all options): decomposing into owner + agreement roles with evidence pointers
(Purver et al.) is the established approach; email commitment models show strong domain
sensitivity (Azarbonyad et al.), so per-owner evaluation data matters more than prompt polish.

Hallucination (all options): the products that auto-create (Gmail, Siri) avoid free-form
inference entirely; in LLM systems, the consistent mitigation in the literature is grounding
every stored fact in retained raw evidence (LongMemEval's "keep raw turn + augment with
facts", Zep's episodic layer), which is what v0.14's verbatim-evidence gate approximates.

## Recommendation (for a single-user, cloud-LLM system)

1. **Adopt option C (per-thread Commitment ledger updated by operations), backed by the raw
   message log (E-lite).** This is the convergent pattern across DST (SOM-DST), and LLM memory
   (Mem0, Zep). It eliminates the re-detection churn that most v0.14 gates and the
   reconciliation stack exist to suppress. Prompt = ledger (with IDs, resolved dates, evidence
   message IDs) + new messages + a short recent window (prior art uses 4–10 messages).
2. **Make operations explicit and evidenced.** Output `CREATE | UPDATE(fields) | CANCEL |
   NOOP`, each citing message IDs; validate citations deterministically. Separate fields for
   owner/participants and *the user's agreement act* (Purver's roles) so "not mine" is a
   checkable field, not a vibe.
3. **Bound error propagation** (the main cost of C): never hard-delete — invalidate with
   timestamps (Zep/Mem0g bi-temporal style) so undo/Corrections can restore; treat a user
   Correction as ground truth that overwrites the ledger entry; and run an occasional full
   re-derivation/audit of a thread's ledger from the raw log (e.g. on Correction, on a
   date-affecting update, or on a slow schedule) to catch sticky errors.
4. **Resolve dates at ingest, in code where possible.** Have the model link a relative
   expression to its anchor (message timestamp by default, or a ledger entry/earlier explicit
   date), and compute the calendar date deterministically from that anchor. Store the resolved
   date + anchor reference on the Commitment.
5. **Keep scope honest.** The vendors don't auto-create from casual chat at all; the
   auto-vs-Suggestion routing threshold is where most precision is won or lost and should be
   tuned on the owner's labeled corpus, scored per failure mode.

## Caveats / what I couldn't verify

- No primary source found that evaluates incremental LLM commitment tracking on personal
  chat specifically; all numbers come from adjacent benchmarks (MultiWOZ, LoCoMo,
  LongMemEval) whose distributions differ from iMessage.
- Gmail and Apple publish *what* they extract, not *how* they track state; nothing
  first-party on cross-message tracking was found.
- The Purver 2007 PDF couldn't be text-extracted here; claims about roles/κ come from the 2006
  paper's text as surfaced in search excerpts and the ICASSP'08 restatement.
- Mem0's numbers are self-reported by the vendor.
