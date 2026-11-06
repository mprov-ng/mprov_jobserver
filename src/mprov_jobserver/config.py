"""Reading the job server's YAML config."""


def merge_config(documents):
  '''
  Flatten a config file's list of documents (`- global: ...`, the
  `!include`d plugin files, ...) into one dict.

  A later document's section replaces an earlier one of the same name,
  except `global:`, whose settings are merged one by one: a plugin file can
  add or change a single global setting (e.g. `caBundle` or `logLevel`)
  without wiping the rest, such as the `mprovURL` and `apikey` that a
  container renders from its environment (#301).  Later documents win.
  '''
  result = {}
  for entry in documents:
    if not entry:
      continue
    for key, value in entry.items():
      if key == "global" and isinstance(value, dict) and isinstance(result.get("global"), dict):
        result["global"] = {**result["global"], **value}
      else:
        result[key] = value
  return result
