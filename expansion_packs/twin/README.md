# Twin Expansion Pack

Two cartridges that turn the console into a twin generator + transporter:

| Cartridge | Tool name | What it does |
|---|---|---|
| [`summon_twin_agent.py`](./summon_twin_agent.py) | `SummonTwin` | Generate a fresh digital twin organism on this device. Picks a soul template based on `kind` (personal / pre-founder / memorial / project / place / custom), mints a fresh `rappid` (UUIDv4), writes `~/.rapp/twins/<rappid>/`, registers in the neighborhood. |
| [`hatch_egg_agent.py`](./hatch_egg_agent.py) | `HatchEgg` | Import a `.egg` cartridge (a twin packed elsewhere) and hatch it as a fully-viable local twin. Identity, memory, and mutations preserved. Same `rappid` as wherever the egg was packed. |

## Install

After running the rappterbox console installer:

```bash
bash ~/.brainstem/installer/install-expansion-pack.sh twin
bash ~/.brainstem/start.sh    # restart to pick up the new cartridges
```

Or install the console + this pack in one shot:

```bash
curl -fsSL https://kody-w.github.io/rappterbox/installer/install.sh | bash -s -- --with twin
```

## Use it (in chat)

> **You:** "I have my dad's twin egg on a USB stick at /Volumes/usb/dad.egg. Hatch it on this machine."
>
> **Model:** *invokes `HatchEgg(egg_path="/Volumes/usb/dad.egg")`*
> "Hatched twin 'dad-twin' (rappid 7bd3...) — fully viable. Workspace: ~/.rapp/twins/7bd3.../. Identity preserved across the substrate hop."

> **You:** "Make me a memorial twin for my grandmother who passed last year."
>
> **Model:** *invokes `SummonTwin(twin_name="grandma-twin", kind="memorial", description="...")`*
> "Created memorial twin 'grandma-twin' (rappid 2af8...). Located at ~/.rapp/twins/2af8.../. Soul.md uses the memorial template, with your description woven in."

## Manage your twin estate

After summoning/hatching, install [`rapp-zoo`](https://github.com/kody-w/rapp-zoo) for a Pokédex-style UI listing all twins on this device:

```bash
curl -fsSL https://kody-w.github.io/rapp-zoo/installer/install.sh | bash
bash ~/.rapp-zoo/installer/start.sh    # opens at http://127.0.0.1:7070
```

## Contract

These cartridges follow the static-ancestor rule: the rappterbox console is the immutable substrate, the cartridges plug into its existing interface and produce organisms in canonical paths (`~/.rapp/twins/<rappid>/`). They never modify the console.
