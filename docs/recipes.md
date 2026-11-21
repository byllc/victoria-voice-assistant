# Recipes

The `recipes` tool answers from a folder of recipe files, compared with the grocery list. When nothing in the folder is close to what you asked for, it looks on the web. Architecture §8 has the design.

## What it does

- "What do I need to make paella?": what you have and what's missing, then "Add those to groceries?"
- "With the groceries I have, what can I make?": up to three recipes from the folder, fewest missing ingredients first.
- "Show me the paella recipe": the whole recipe, as a card in the chat.
- "Save that recipe": keeps a recipe it found on the web. It never saves without being asked.

## The folder

One Markdown file per recipe, in `~/Library/Application Support/Victoria Voice Assistant/recipes/` (or `recipes.folder` in `config/tools.yaml`). `_template.md` in the folder shows the shape:

```markdown
# Paella
Serves: 6
Time: 1 hour 15 minutes
Tags: dinner, spanish, rice

## Ingredients
- 2 cups bomba rice
- 1 pinch saffron

## Steps
1. Heat the stock with the saffron.

## Notes
Source: https://…
```

Only the title and the Ingredients list are required. To see what's in the folder and which files don't fit:

```bash
uv run assistant recipes check
```

The nightly backup carries the folder when it's in the default place. A folder you set elsewhere (iCloud Drive, say) is yours to back up.

## What counts as "have"

- A grocery item **checked off in the last 14 days** (`recipes.have_days`). It covers an ingredient when all of the item's words are in it: Rice covers "bomba rice".
- A **staple** (`recipes.staples`: salt, pepper, oil …), which only counts for the same thing: "black pepper" yes, "red bell pepper" no.

The match can be wrong (Milk would cover "coconut milk"). When it counted a differently named item, the answer says which: "bomba rice (your Rice)".

## Settings

```yaml
recipes:
  # folder: ~/Documents/Recipes
  groceries_tool: groceries
  have_days: 14
  staples: [salt, pepper, water, oil, olive oil, butter, sugar, flour]
```

Web recipes come from pages that publish their recipe as structured data, which most recipe sites do. A page without it is passed over.
