# Beginner Tutorial

This is the simple version. You only need WAV files and ChromaKit.

## Make a Chromatic

1. Create a new folder.
2. Put your voice samples in that folder.
3. If you can, name them `1.wav`, `2.wav`, `3.wav`, and so on.
4. Open ChromaKit.
5. Stay on the **Generate** tab.
6. Click **Browse** and choose your sample folder.
7. Leave the default settings alone for your first try.
8. Click **Generate Chromatic**.

When it finishes, look inside your sample folder. You should see `chromatic.wav`.

## Prepare Raw Vocals First

Use this if you have a long recording instead of clean short samples.

1. Open ChromaKit.
2. Go to **Prepare Samples**.
3. Click **Choose WAV Files** for one or more files, or **Choose Folder** for a folder.
4. Leave the silence settings alone for your first try.
5. Click **Prepare Samples**.
6. Open the new `prepared_samples/` folder.
7. Go back to **Generate**.
8. Choose the `prepared_samples/` folder.
9. Click **Generate Chromatic**.

## What the Main Generate Settings Mean

- **Starting note**: the first note in the scale.
- **Starting octave**: how low or high the first note starts.
- **Range**: how many notes ChromaKit makes.
- **Sample gap**: silence between notes in `chromatic.wav`.
- **Sample order**: how samples are picked.
- **Pitch samples**: changes each sample to match the notes.
- **Audio style**: chooses the retuning character. **Formant corrected** is the clean default and preserves vocal formants.
  - **Vocal strain** gradually adds a small formant lift, harmonic drive, and presence after a source sample is pitched more than four semitones upward.
  - **Bright belt**, **Scream / belt**, and **Rasp** build on the direct **Praat** pitch-resynthesis character, then add progressively stronger drive, presence, and (where applicable) controlled vocal roughness. They respond smoothly to the upward interval, so low notes remain clean. They work best with a clean, voiced vocal source; they are audio effects, not AI voice generation.
- **Dump individual samples**: saves separate WAV files too.
- **Trim silence from samples**: cuts quiet edges from samples.
- **Peak normalize before pitch**: makes sample volume more even.
- **Embed FL Studio Slicex markers**: adds markers for FL Studio Slicex.

## If It Does Not Work

- Make sure your files are `.wav`.
- Make sure the folder is not empty.
- Try shorter, cleaner samples.
- If **Prepare Samples** finds nothing, lower the silence threshold, for example from `-40 dB` to `-50 dB`.
- If the output sounds messy, try fewer samples first.

## Loop Samples

Use the **Loop Samples** tab to turn one or more WAV files into repeatable samples:

1. Click **Add WAV Samples** (or **Add Folder**).
2. Set the seam **Crossfade**. The default 30 ms is a good starting point; longer values make a softer loop transition.
3. Click **Save Looped Samples**.

ChromaKit trims optional quiet edges, blends each file’s tail into its head, and writes `<original-name>_loop.wav` files in a `looped_samples/` folder beside the source. The loop boundary lands on consecutive waveform samples, eliminating the hard boundary click when the file repeats.


## Test a Chromatic with the Keyboard

The **Keyboard** tab previews exported note WAVs without leaving ChromaKit. Choose the generation folder (the app automatically uses its `pitched_samples/` folder) or choose that folder directly. Set the first note to match the chromatic, then click any of the 24 note buttons. You can also use two physical-key rows: `Z S X D C V G B H N J M` and `Q 2 W 3 E R 5 T 6 Y 7 U`. Generate with **Dump individual samples** enabled before testing.
