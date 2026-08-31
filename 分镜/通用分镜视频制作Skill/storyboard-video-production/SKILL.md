---
name: storyboard-video-production
description: Turn scripts or scene descriptions into production-ready storyboard clips, continuous video shots, audiovisual timelines, and model-adapted generation prompts. Use for creating, revising, or checking storyboards, shot plans, continuity, pacing, or video prompts; do not use for unrelated image generation or prose-only script writing.
---

# Storyboard Video Production

Convert narrative material into a traceable visual production plan without binding the result to one project schema or media model.

## Select the requested finish line

Determine whether the user wants:

- director planning or a shot list only;
- storyboard prompts or generated storyboard images;
- `Clip -> Shot` splitting and continuity review;
- video-generation prompts;
- actual media generation or regeneration.

Stop at the requested finish line. Do not invoke costly media generation merely because prompts were requested. If the user asks for an end-to-end result, continue through every available stage and clearly identify unavailable capabilities.

## Gather production constraints

Use provided script, viewpoint, aspect ratio, visual references, character/scene/prop assets, dialogue, desired style, delivery format, and target model limits. Preserve explicit creative choices.

Do not block early creative planning on optional production metadata. Mark assumptions and distinguish a draft plan from a production-ready plan. Before reference-bound generation, require real accessible references rather than inventing asset IDs, URLs, voices, or files.

When a target video model matters, establish its supported duration, aspect ratio, reference types, audio behavior, prompt language, and other material limits from available authoritative information. Do not inherit the archived original's 15-second ceiling as a universal rule.

## Production workflow

1. **Interpret the complete narrative**
   - Preserve event order, causal information, dialogue or inner voice, relationship changes, emotional turns, reveals, choices, and required audience knowledge.
   - Diagnose the dramatic purpose of each beat before choosing camera language.

2. **Create a director plan**
   - Divide time, place, and viewpoint into scenes.
   - Establish scene geography, entrances, exits, obstacles, light, weather, screen direction, dialogue axis, character goals, blocking, prop ownership, and sound coverage.
   - Choose framing and movement because they serve the beat, not to decorate the prompt.

3. **Plan storyboard Clips**
   - Treat a `Clip` as one representative still state at one clear moment, not automatically as one video shot.
   - Give every Clip a stable ID, dramatic development, visible state, necessary sound landing, camera view, and continuity relation.
   - Map every required script beat to at least one Clip. Split any Clip that contains incompatible time states, staging, or compositions.

4. **Lock or revise the storyboard**
   - Before generating a storyboard, check coverage, single-state representability, physical feasibility, and adjacent continuity.
   - Once a storyboard version is approved or selected, treat its Clip order and state as immutable input to Shot planning. If the script, assets, or storyboard requirements change, create a new version instead of silently patching downstream prompts.

5. **Group adjacent Clips into Shots**
   - A `Shot` is one continuously generatable audiovisual unit and may contain one or more adjacent Clips.
   - Mark hard boundaries for time/place/viewpoint changes, incompatible axes or camera sides, impossible physical transitions, required independent camera setups, deliberate reveal cuts, or content exceeding the target model's natural capacity.
   - Between hard boundaries, combine adjacent Clips only when one dramatic objective, continuous blocking, compatible camera behavior, and uninterrupted sound can carry them naturally.
   - Build a one-to-one ordered `Clip -> Shot` mapping. Every Clip must appear exactly once.

6. **Build continuity before parallel work**
   - Give each Shot a stable ID, source Clip range, narrative description, duration estimate, start state, end state, and required references.
   - Track scene anchors, camera side, screen direction, character position and body state, prop ownership/state, lighting, weather, and continuous sound.
   - A same-scene Shot inherits the previous Shot's end state unless the narrative explicitly changes it.

7. **Compile model-adapted prompts**
   - Plan the visual timeline and sound timeline independently; their boundaries need not coincide.
   - Keep dialogue intact across visual changes. Do not restart a line because the image state changes.
   - Convert analysis into direct, observable instructions. Remove rationale, risk discussion, symbolism explanations, and internal quality fields from execution prompts.
   - Adapt syntax and reference notation to the selected model. Never emit platform-specific tokens unless that platform actually supports them.

8. **Execute reliably when generation is requested**
   - Initialize the complete ordered result list before parallel tasks.
   - Give each worker one immutable Shot record plus shared director context. Write results back by stable Shot ID, never by completion order.
   - Preserve failed or unfinished slots as explicit empty results; do not delete, append, or reorder them. Retry only the affected Shot unless an upstream invariant changed.

## Output

Use the user's requested schema when supplied. Otherwise return the smallest useful combination of:

- storyboard/Clip plan;
- Shot plan with stable IDs and source Clip mapping;
- concise creator-facing Shot descriptions;
- video prompts;
- generated media references;
- continuity or failure notes.

Keep internal reasoning, validation matrices, and model parameters out of the main creative deliverable unless the user asks for them.

## Detailed guidance

Read [references/production-principles.md](references/production-principles.md) when planning non-trivial dialogue, multi-character blocking, action continuity, storyboard prompts, audiovisual timing, parallel generation, or quality review.

The archived source beside this Skill is provenance and comparative study material, not a runtime dependency. Read it only when the user explicitly asks to compare with or reproduce the original implementation.
