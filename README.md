# AI Proposal Triage Design (MATACS-TH-2026-02)

| | |
|---|---|
| Name | Sudais Khan |
| Assessment reference | MATACS-TH-2026-02 |
| Time actually spent | About 3 hours |

This is a design for a tool that turns uploaded contractor proposals into a ranked, explained shortlist. **AI reads the proposals, code applies the rules and arithmetic, and people approve the facts and make the award.**

## Deliverables

| File | What it contains |
|---|---|
| [diagram/process-flow.png](diagram/process-flow.png) | Process flow from category set-up and upload to ranked output, with AI, rule-based and human steps shown separately |
| [WRITEUP.md](WRITEUP.md) | Answers to the five questions, plus the scoring rubric, assumptions and sample decisions (under 1,000 words) |
| [PROMPTS.md](PROMPTS.md) | The exact extraction prompts, the JSON output contract and the repair prompt |
| [ranking.json](ranking.json) | Expected ranking of proposals A–E |
| [prototype/](prototype/) | Optional deterministic scorer: [triage.py](prototype/triage.py), [test_triage.py](prototype/test_triage.py), [criteria_snapshot_v1.json](prototype/criteria_snapshot_v1.json), [approved_facts.json](prototype/approved_facts.json) |
| [.env.example](.env.example) | Confirms that no API keys or secrets are needed |

**Expected result:** A ranks 1st (72.96) and E 2nd (64.56). C is flagged and held for security review because it contains an instruction aimed at automated reviewers; its points are unaffected, and it would rank 3rd (56.40) once cleared. D is flagged because it gives no completion schedule. B is ineligible because the bidder does not hold the required licence itself.

## How to review

1. Look at the [diagram](diagram/process-flow.png) for the whole flow (about 2 minutes).
2. Read [WRITEUP.md](WRITEUP.md): assumptions, rubric and sample results first, then the five answers.
3. Skim [PROMPTS.md](PROMPTS.md) to see exactly what the AI is asked to do and what it may return.
4. Check [ranking.json](ranking.json). Optionally, re-run the scorer to reproduce it (below).

## Running the prototype (optional)

Python 3, standard library only (tested with Python 3.12). No API key, network access or install step. From the repository root:

```bash
cd prototype
```

Print the full breakdown: gates, each criterion's score and contribution, warnings and blockers.

```bash
python triage.py
```

Confirm that `ranking.json` matches the rubric (exits with code 1 if it does not):

```bash
python triage.py --check ../ranking.json
```

Run the tests:

```bash
python -m unittest -v test_triage
```

On Windows, use `py` instead of `python` if `python` is not on your PATH.

**What the prototype covers.** It runs the deterministic half of the design (config validation, gates, scoring, ranking and the extraction-output validator) on fact sheets that stand in for reviewer-approved AI extractions. It does not call an AI model. The brief asks that proposal text is not copied into the repository, so the extraction prompt has not been run on the samples here. The tests use invented data to check budget and deadline exactly at and just above their limits, missing and contradictory inputs, invalid weights, ties, and that instruction-like document text cannot become a scoring input. They do not test how a live model resists prompt injection.

## AI assistance

I used Claude (Anthropic), running as Claude Code, to draft this repository from a detailed task specification I gave it. It read the brief, proposed the rubric, wrote the documents and prototype, rendered the diagram and ran the checks described above. Claude worked from a low-resolution screenshot of the brief, so I checked the budget ceiling, Proposal A's workmanship warranty and Proposal B's price against the original PDF myself. I also read WRITEUP.md and PROMPTS.md in full, ran the scorer and unit tests on my own machine, and went through the diagram step by step against the process it describes.

**Where I disagreed with or corrected the AI:** Claude's first version scored Proposal C normally and only attached a warning about the instruction hidden in its appendix. I changed the security policy to hold instead. The instruction had no effect on the points, but a bid that tries to steer the evaluator should not sit in a ranked shortlist until a person has checked it. C is now flagged rather than ranked 3rd. If a reviewer clears it, it would score 56.40 and rank 3rd, so the change affects its status, not the arithmetic. Both options remain in the config (`hold` or `score_with_warning`).

## Notes

- The brief and the sample proposal text are not reproduced here. Proposals are referred to by letter, and [approved_facts.json](prototype/approved_facts.json) holds only the values transcribed from them.
- All companies and details in the brief are fictional. No real client or personal data is used.
