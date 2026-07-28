"""Cutting keystrokes out of what the terminal actually sends.

A terminal writes an arrow as three bytes in one go. The dashboard used to read
one byte through sys.stdin and then wait on select() for the next - but the
buffered read had already taken all three, so select() saw an empty file
descriptor and the "[" came back later as a keystroke. "[" is the group-demote
key, so every arrow press printed "not in a group".

No timeout fixes that: the bytes were never late, they were already read. These
tests feed whole writes in and check exactly one key comes off the front.
"""
from harness import bn as m

FAILED = []


def check(label, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + label.ljust(50)
          + ("" if ok else f"\n         got  {got!r}\n         want {want!r}"))
    if not ok:
        FAILED.append(label)


def keys(buf):
    """Every keystroke in one write, the way the loop takes them."""
    out = []
    while buf:
        key, buf = m.split_key(buf)
        if not key:
            break
        out.append(key)
    return out


print("\none write, one key")
check("a plain letter", m.split_key("j"), ("j", ""))
check("an arrow comes off whole", m.split_key("\033[A"), ("\033[A", ""))
check("a page key too", m.split_key("\033[6~"), ("\033[6~", ""))
check("and one with a modifier in it", m.split_key("\033[1;5A"), ("\033[1;5A", ""))
check("nothing sent, nothing read", m.split_key(""), ("", ""))

print("\nthe bug: a whole arrow in a single read")
check("three bytes are one keystroke, not three",
      keys("\033[A"), ["\033[A"])
check("so no stray bracket is ever delivered",
      "[" in keys("\033[A"), False)
check("nor a stray letter", "A" in keys("\033[A"), False)

print("\nseveral keys in one write - a held-down arrow, or a fast typist")
check("two arrows", keys("\033[B\033[B"), ["\033[B", "\033[B"])
check("an arrow then a letter", keys("\033[Bq"), ["\033[B", "q"])
check("a letter then an arrow", keys("q\033[B"), ["q", "\033[B"])
check("letters keep their order", keys("jkG2"), ["j", "k", "G", "2"])

print("\nthings that are not arrows")
check("Esc alone stays Esc", m.split_key("\033"), ("\033", ""))
check("Alt and a letter is one key", m.split_key("\033d"), ("\033d", ""))
check("a sequence cut off mid-flight is dropped, not half-delivered",
      m.split_key("\033[1;"), ("", ""))
check("Enter is Enter", m.split_key("\r"), ("\r", ""))

print("\nwhich way a sequence points")
check("up", m.arrow_of("[A"), "up")
check("down", m.arrow_of("[B"), "down")
check("right", m.arrow_of("[C"), "right")
check("left", m.arrow_of("[D"), "left")
check("page up", m.arrow_of("[5~"), "pgup")
check("page down", m.arrow_of("[6~"), "pgdn")
check("the application-mode spelling of up", m.arrow_of("OA"), "up")
check("ctrl with an arrow is still that arrow", m.arrow_of("[1;5A"), "up")
check("shift with a page key too", m.arrow_of("[6;2~"), "pgdn")
check("shift-tab is not an arrow", m.arrow_of("[Z"), "")
check("junk is not an arrow", m.arrow_of("nonsense"), "")



print("\nquitting takes two presses")
# The dashboard is the only view of the fleet and the only thing maintaining
# the queue's bindings and the status-line counter. One key next to nothing
# else you press is too little to hang that on.
q1 = m.quit_press(0.0, now=1000.0)
check("one press does not leave", q1[0], False)
check("it starts the clock", q1[1], 1000.0)
check("and says what is being asked", q1[2], m.QUIT_NOTE)
check("a second press soon after leaves",
      m.quit_press(1000.0, now=1002.0)[0], True)
check("and takes the question down with it",
      m.quit_press(1000.0, now=1002.0)[1:], (0.0, ""))
# A q pressed a minute later is not an answer to a question you have forgotten
# you were asked - it is the first press again.
late = m.quit_press(1000.0, now=1000.0 + m.QUIT_CONFIRM + 1)
check("a press after the window asks again rather than quitting",
      (late[0], late[2]), (False, m.QUIT_NOTE))

print("\nand it is not a line you can read past")
rows = []
plain = m.render(rows, width=60, note="parked", auto=True)
loud = m.render(rows, width=60, note=m.QUIT_NOTE, auto=True, loud=True)
check("an ordinary note is a line of yellow", "\033[1;93m" in plain, True)
check("a loud one is a bar", "\033[1;97;41m" in loud, True)
check("across the whole pane",
      max(len(l) for l in loud.split("\033[1;97;41m")[1].split("\033[0m")[0]
          .split("\n")), 60)
check("saying what it wants", "press q again" in loud, True)

print("\nall pass" if not FAILED else f"\n{len(FAILED)} FAILED")
raise SystemExit(1 if FAILED else 0)
