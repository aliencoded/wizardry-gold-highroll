# How I Taught a 1992 Game to Roll Me a Faery Ninja

*A plain-English story about cheating fairly at a very old, very stubborn game.*

## The dream

In *Wizardry 7*, when you make a character you pick a race and then the game
secretly rolls some dice and hands you a pile of "bonus points." You spend those
points on your character's stats. The catch: you can only choose a profession if
your stats are high enough for it.

I wanted a **Faery Ninja**. Faeries are tiny magical creatures with famously weak
bodies, and Ninja is one of the most demanding jobs in the game. To qualify, you
need a *huge* pile of bonus points — the kind the game hands out maybe once in
hundreds of tries. People literally spend hours re-rolling for it. It's a running
joke in the community that a Faery Ninja is basically impossible to roll.

I didn't want to spend my evening re-rolling dice. So I tried to cheat. That's
where the fun started.

## Attempt 1: just change the number

The bonus points are stored somewhere in the computer's memory while the game
runs. I wrote a little tool to find that number and change it. Found it, set it
to a big value... and the number on screen changed, but the game **still wouldn't
let me pick Ninja.**

Weird. The number was high. Why no Ninja?

## Attempt 2: hold the number up by force

Maybe the game kept resetting my change. So I made my tool slam the high number
back in, over and over, hundreds of times a second. Still no Ninja. The number was
clearly high — the game just didn't care.

## Attempt 3: freeze the game mid-roll (and crash it)

I got more aggressive. I attached a debugger — a tool that can freeze a program at
an exact instant — to catch the moment the game rolled the dice and slam my value
in right then.

The game crashed. Hard.

At this point I stopped poking and did some homework.

## The realization

I researched how other people had beaten this, and read the game's behavior more
carefully. The truth finally clicked:

> The game decides which professions you're allowed to pick **at the exact moment
> you choose your character's gender**, based on the dice roll it makes right
> then. After that, the decision is locked. Changing the number afterward only
> changes what you *see* — the game already made up its mind.

So editing the number was always pointless. It's like changing the final score on
a scoreboard after the game's over. The scoreboard says you won, but you didn't.

To actually win, I couldn't change the *number*. I had to change the **dice roll
itself** — or rather, the part of the game that does the rolling.

## Reading the game's "recipe"

Every program is, deep down, a list of tiny instructions. With the right tools you
can read them — like reading the recipe a chef follows, step by step. So I opened
up the game's program file and found the exact recipe it uses to roll your bonus
points.

Here's the beautiful part. The recipe, translated into plain English, roughly
said:

> Roll a small starting number (about 5 to 11). Then... **if a certain hidden
> switch is flipped ON, throw all that away and roll a big number between 21 and
> 30 instead.**

There was a secret "give this character an amazing roll" switch built right into
the game. And right next to it, another instruction that says: *"keep this switch
turned OFF."*

In other words, the developers built a cheat for themselves — a way to instantly
roll a god-tier character for testing — and then disabled it before shipping the
game. It had been sitting there, dormant, for over 30 years.

## The fix: flip the switch back on

I didn't even need the hidden switch. I just changed the one instruction that
said *"if the switch is off, skip the big roll"* into *"do nothing"* — so the game
now **always** takes the big-roll path. Every character creation now rolls 21–30.

It's a six-letter change to the game's recipe. That's it.

And because the game is now genuinely rolling a high number — using its own dice,
its own code — everything downstream just works. The game checks "is this roll big
enough for Ninja?", sees a 27, and happily says yes. No crashing, no fighting, no
faked scoreboard. A real high roll, every time.

## The result

Close the game, run a tiny script that flips that one instruction (it makes a
backup first, and you can undo it anytime), relaunch, and create your character:

**Name → Faery → gender → and the bonus points come up 21–30 → Faery Ninja is
right there, waiting to be picked.**

The thing the community calls nearly impossible now happens on the first try.

## What I actually learned

- The "obvious" cheat (change the number) failed because I misunderstood *when*
  the game made its decision. Hours of effort, all aimed at the wrong moment.
- The breakthrough came not from a cleverer hack, but from **reading what the
  program actually does** instead of guessing.
- Old games are full of forgotten developer shortcuts. Sometimes the best "hack"
  is just switching one back on.

The full technical version — addresses, disassembly, the exact bytes — is in
[WRITEUP.md](WRITEUP.md). The tool is in this repo.
