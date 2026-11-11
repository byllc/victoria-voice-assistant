# Voice turns while an agent uses the GPU

- Status: open
- Phase: staging
- Origin: Architecture review, 2026-11-11 (architecture §12, shared GPU)
- Created: 2026-11-11 (UTC)

## Done when

- [ ] On the Mac mini, measure a voice turn's latency while Hermes runs a long local-model task, against the same turn with the GPU idle.
- [ ] Bill accepts the result, or a fix is agreed (architecture §12: pause background agent work during a voice turn, or move heavy agents to a cloud model).
- [ ] Cancelling a running agent task by voice works live (on the M2, routing "cancel the dinner plan task" took longer than the task itself; carried from Stage 7).

## Blocking decisions

- Open question 2: which model Hermes uses by default.

## Evidence and notes

Not started. Hermes shares the router's loaded copy of the model through LM Studio's parallel slots; nothing gives a voice turn priority.
