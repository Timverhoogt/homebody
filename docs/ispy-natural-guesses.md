# Fair I Spy guesses

I Spy judges whether a guess identifies the selected object, not whether a child repeats the full stored phrase.

- The basic noun can be enough: `car` for `yellow toy car`, or `ballon` for `rode ballon`.
- Natural questions, ordinary synonyms, plurals and diminutives are accepted when they identify the same object.
- Optional colour, size, brand, material and location do not become required answer words. Explicitly contradictory details and unrelated objects are not automatically accepted.
- The judge receives the current round's retained name, category, colour, location and descriptive hints. A visual label is fallible: a more precise toy name can be accepted when those observations support it. Wheels alone do not establish that a toy is a car.
- A successful guess gets a brief confirmation without reasserting the stored visual label. Accepting a child's better name must not be followed by insisting that the imperfect original name was correct. Give-up/failed-round reveals still use the retained round target rather than silently switching objects.
- Visual selection asks for a basic object name with colour stored separately. For uncertain toy subtypes it asks for an honest broader name rather than invented precision. This is provider guidance, not a new local object blacklist or confidence threshold.

The model still makes the semantic decision; this is not a substring/colour-stripping matcher. Round identity, Stop/End/privacy, camera grants, input/output moderation and exact speech approvals remain enforced outside the judge. Guess and observation values are untrusted user-role JSON, separate from the judging instructions.

Diagnostics record only the judge's boolean response, not guesses, labels or transcript text. That response is not proof that a subsequently cancelled turn committed a result.

## Verification limits

Synthetic route/transport tests establish the contract, moderation and target retention. Text-only real-provider probes establish sampled semantic behaviour, not microphone recognition or identification of an unseen real-room toy. The old judge already accepted simple noun-only car/balloon examples in a fresh probe; without retained historical transcripts, the original exact-phrase failures cannot be reconstructed. An empty or misheard transcription remains a separate issue, and child audio/text retention is not enabled by this change.
