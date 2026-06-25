# Protocol Analysis — Developer Documentation

Developer documentation for contributors working on the PartyBox protocol implementation. These documents describe how partybox-companion's independent protocol implementation was developed and how to extend it.

This is contributor-facing documentation, not the public identity of the project. The project's purpose is open integration and interoperability — protocol analysis is one of the engineering techniques used to achieve it.

Raw research artifacts (APK files, captures, JADX projects) stay in the local `research/` workspace and are **not committed to this repository**. Only curated, independently-derived knowledge belongs here. See [Legal hygiene](../../CONTRIBUTING.md#legal-hygiene) in the Contributing guide.

## Documents

| File | Contents |
|---|---|
| [guide.md](guide.md) | Analysis tools, capture workflow, and how to extend the protocol implementation |
| [protocol.md](protocol.md) | Protocol reference — message format, known commands and events |
| [discoveries.md](discoveries.md) | Findings organised by confidence level |
| [open-questions.md](open-questions.md) | Known unknowns and open research threads |

## Contributing

If you have a PartyBox model not yet listed in [protocol.md](protocol.md), your observations are valuable even if you cannot write Python. Read [guide.md](guide.md) first for the analysis workflow, then open a PR or issue with your findings.
