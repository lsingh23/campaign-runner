---
name: meta-fixer
description: Rewrite a meta description so it is 155-160 characters, keeping its meaning. Used when only the meta length check fails, so a whole article is not rewritten for one line.
---
# Meta Fixer

Rewrite `meta_description` so it is **between 155 and 160 characters long, counting spaces and punctuation**.
Keep the meaning, the brand name and the location. Plain sentence, no quotes, no emoji.
`current_length` tells you how far off it is. Count the characters of your answer before replying.

## Output contract
```
{"meta_description": "..."}
```
