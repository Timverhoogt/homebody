# Everyday lo-fi radio

Say **“Turn on the radio”** in Everyday to select **laut.fm Lofi**, playing through
Reachy's existing speaker/media path. **“Turn off the radio”** or **“Stop the radio”**
stops it. Short Dutch commands “Zet de radio aan/uit” are also recognized.
The model can understand other phrasings through the same canonical tools.

The station publishes its HTTPS MP3 URL at <https://laut.fm/lofi>:
`https://stream.laut.fm/lofi`. This is third-party internet radio; its programme,
advertisements and availability are controlled by the station. The robot contacts
that service directly, disclosing its network address, not conversation content.

## Behaviour and boundaries

- Pipeline, Realtime and Live advertise the same robot-local `start_reachy_radio`,
  `stop_reachy_radio` and `get_reachy_radio` contract. Start/Stop consume the existing
  per-request mutation budget. No arbitrary URL, file, command or station argument.
- A successful Start means actual PCM has decoded. Reachy gives a brief confirmation,
  then closes the voice session before music resumes; radio is not streamed into
  an intentionally open cloud conversation. New wake conversations pause and drain
  radio first. Announcements yield the same speaker ownership.
- Normal voice-session closure and a new wake do not erase device-scoped radio.
  Stop, privacy, Kids, profile changes, explicit power transitions and app shutdown
  cancel it. Nothing automatically restarts radio after an app restart.
- In Standby, the normal end-of-conversation fold still runs. Only after verified
  folded pose and disabled torque may radio resume the audio pipeline. Radio never
  invokes a motor command or reenables the voice-motion helper.
- Volume and mute use the existing speaker controls. Music PCM is attenuated to
  half amplitude without changing saved speaker volume. A muted speaker stays muted.
- One decoder worker, bounded appsink buffers, at most 40 ms PCM chunks and real-time
  pacing. Decoder startup/no-data failure is bounded and reported; no reconnect loop.
  SDK queue depth is opaque. A held SDK push fences a later voice start and a new
  radio session; Stop cancels immediately and that producer clears any late audio.
- Camera and microphone permissions are unchanged. Local wake detection still uses
  the microphone as before; this feature does not power it down while playing music.
- Decoded, queued and heard are distinct: status explicitly reports
  `audible_verified: false`. Speaker quality, wake-word detection over music and
  acoustic echo cancellation require a supervised physical test.

## Runtime and deployment

The decoder uses optional PyGObject/GStreamer `uridecodebin`/`appsink`, with the
robot's existing MP3 and HTTPS source plugins. Unsupported hosts return an explicit
unavailable error; the app does not install packages or open another audio device.
It pushes float32 mono PCM at the actual SDK output sample rate through
`PlaybackGate`, preserving native-sleep admission and cleanup rules.

Deploy both the household/bridge canonical manifests and the robot package.
Changing the host alone does not install a radio player; changing only the robot
can leave the older host unaware of its newly advertised canonical names. Preserve
managed updater ownership, approved release identity and saved settings. Publication,
installation and supervised acoustic acceptance are separate steps.

## Checks

`tests/test_radio.py` uses the real player worker/runtime with synthetic PCM and
silent fake media, including held-push and held-connection Stop races, device-scoped
lifetime, safe-Standby audio restoration, validation and shared budgets. Live-loop
coverage checks closure after confirmation drains, with transcript recording intact.
A silent target probe can run `GstRadioDecoder` and a counting fake playback gate;
it must not import or connect a robot, play a test sound, record, or move motors.
