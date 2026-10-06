# Model library

Default place where the app looks for trained SLEAP models (each a folder with
`training_config.json` and `best_model.h5`). Models here appear in the
*New project* and *Settings → SLEAP models* pickers. Add a model by copying its
folder here or by linking to it:

```bash
ln -s /path/to/my_model.single_instance.n=300 models/
```

Everything in this folder except this file is ignored by git. Set
`FEEDING_MODEL_LIBRARY` (folders separated by `:`, or `;` on Windows) to use other folders.
