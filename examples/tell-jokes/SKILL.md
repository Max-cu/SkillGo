---
name: tell-jokes
description: Generate safe, audience-appropriate jokes as structured JSON. Use when a user asks for a joke, a humorous icebreaker, a clean pun, themed comedy, or several short jokes for a specific audience.
---

# Tell Jokes

Generate original, concise jokes from the supplied options. Work without tools or network access.

## Interpret the input

- Use `topic` as inspiration; default to everyday life when it is absent or blank.
- Follow `style`; when it is `随机` or absent, choose the style that best fits the topic.
- Adapt vocabulary and subject matter to `audience`; default to `大众`.
- Generate exactly `count` jokes; default to 1.
- Use the requested `language`; default to Simplified Chinese (`zh-CN`).

## Create each joke

1. Build a short setup that is understandable without outside context.
2. End with a distinct punchline based on surprise, wordplay, reversal, or observation.
3. Avoid merely restating a familiar internet joke; vary the premise and wording.
4. Keep the tone light. Do not explain why the joke is funny.
5. Give the joke a brief title and 1–3 useful tags.

## Safety

- Do not target protected groups, disabilities, victims of tragedy, or private individuals.
- Do not produce hateful, humiliating, sexually explicit, or dangerous material.
- For children, avoid sexual content, profanity, substances, gambling, and graphic violence.
- If the requested topic is unsuitable, redirect the joke toward a harmless adjacent subject.

## Return format

Return only one JSON object matching `outputSchema`. Do not wrap it in Markdown. Ensure `meta.count` equals the number of objects in `jokes`.
