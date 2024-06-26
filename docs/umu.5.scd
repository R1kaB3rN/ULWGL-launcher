umu(5)

# NAME

umu-run - configuration file

# DESCRIPTION

umu-run can optionally read a configuration file instead of reading
environment variables set from the command line. The configuration file for
umu-run must be written in *Tom's Obvious Minimal Language (TOML)*[1].

In the configuration file, the table *umu* is required as well as the keys
*prefix*, *proton* and *exe* which all must be basic strings. e.g.:

```
[umu]
prefix = "~/WINE/epic-games-store"
exe = "~/EpicGamesLauncher.exe"
proton = "~/.local/share/Steam/compatibilitytools.d/GE-Proton30"
```

Afterwards, run the command:
```
$ umu-run --config config.toml
```

# [umu] SECTION

*exe*
	Path to the game executable to run. Value must be a basic *string*.

*proton*
	Path to a Proton directory. Value must be a basic *string*.

*prefix*
	Path to a WINE prefix directory. Value must be a basic *string*.

*game_id*
	The umu id to assign to the game. Depending on the value assigned, a proton
	fix will be applied to the prefix if available. Please refer to the *umu
	database*[2] for an extended list of game ids.

*store*
	The distribution platform of the executable. Value must be a basic *string*.
	Expects the values: egs, gog, battlenet, amazon, humble, itchio, and ubisoft.

*launch_args*
	Launch arguments for the executable. Value must be a basic *string* or an
	*array* of basic strings. When using a string, assumes each argument is
	space-separated.

# NOTES

. https://toml.io/en/v1.0.0
. https://github.com/Open-Wine-Components/umu-database/blob/main/umu-database.csv

# AUTHORS

Maintained by Open Wine Components members, and assisted by other open source
contributors. For more information about umu development, see
https://github.com/Open-Wine-Components/umu-launcher.
