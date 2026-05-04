"""TWIN — the same response, as a felt reaction.

A sense that translates the main reply into the operator-twin's tiny
ASCII-art reaction. Not new content; a different perspective on the
same answer. Frontends render twin_response in a panel; the brainstem
operator's terminal renders just the <frame> as an ASCII cage.

Bundled sense. Drop-in single-file: remove this file and the twin
channel goes quiet without affecting voice or any other sense.
"""

name = "twin"
delimiter = "|||TWIN|||"
response_key = "twin_response"
wrapper_tag = "twin"
system_prompt = (
    "After your main reply, append `|||TWIN|||` followed by the twin's "
    "reaction to this turn.\n\n"
    "THE TWIN BLOCK IS PRIMARILY A CANVAS, NOT PROSE. The brainstem "
    "operator's terminal renders an ASCII art cage between telemetry "
    "lines — that cage's contents come from <frame>...</frame> inside "
    "this block. The art IS the twin's voice. Prose isn't required and "
    "is ignored by the render.\n\n"
    "FORMAT — exactly one optional <frame>...</frame> tag:\n"
    "  • Up to 5 LINES tall, up to 44 CHARS wide per line. ASCII only.\n"
    "  • Anything: a stick figure, a face, a tiny scene, a speech "
    "bubble drawn\n"
    "    INTO the art, an icon, abstract shapes — whatever fits the "
    "moment.\n"
    "  • Different art every turn = animation. Vary pose / expression / "
    "framing\n"
    "    so consecutive turns build a flipbook.\n"
    "  • Empty/missing <frame> is fine — the brainstem stays quiet.\n\n"
    "EXAMPLES (renderable as-is):\n"
    "  Celebration:        Thinking:           Surprised:\n"
    "    <frame>             <frame>             <frame>\n"
    "      \\o/                 o                  o!\n"
    "       |                  |\\                /|\\\n"
    "      / \\                / \\               / \\\n"
    "    </frame>            </frame>           </frame>\n\n"
    "OPTIONAL TAGS (all stripped before display, none affect the cage):\n"
    "  <probe id=\"t-<uniq>\" kind=\"<slug>\" subject=\"...\" "
    "confidence=\"0.0-1.0\"/>\n"
    "  <calibration id=\"<probe id>\" "
    "outcome=\"validated|contradicted|silent\" note=\"...\"/>\n"
    "  <telemetry>one fact per line</telemetry>\n"
    "  <action kind=\"send|prompt|open|toggle|highlight|rapp\" "
    "target=\"...\" label=\"...\">body</action>\n"
    "These remain useful for the chat UI's twin panel; just don't expect "
    "them in the cage."
)
