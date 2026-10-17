# Use Ollama on a Windows PC, Mac or Linux computer

Homebody can use models served by [Ollama](https://ollama.com/download) for **Everyday text reasoning** and **camera vision**. Ollama does not have to run on the computer driving Reachy. A Wireless, Pi or Jetson robot can use a Windows PC or Mac elsewhere on the trusted home network.

This is a model-server connection, not a requirement to install Homebody or a second Hermes on that PC/Mac. Keep your existing robot and bridge setup. A dedicated `ollama` text-provider preset is included in this source candidate; it is not part of older installed releases.

## What becomes local?

| Feature | Ollama role | What this does not replace |
|---|---|---|
| Everyday in **Pipeline** | Conversation reasoning and the existing bounded tool loop | Speech recognition and speech synthesis have separate providers |
| Camera Ask / configured local camera looks | A vision-capable model describes one authorized image | Camera consent, explicit capture and Stop/privacy protections stay unchanged |
| Realtime / GPT-Live | Local camera analysis can still be used where configured | The live speech session still uses its configured cloud service; selecting Ollama does not reroute it |
| Optional Agent/Kids/I Spy text | The bridge's shared text provider can also select Ollama | Models must pass their own structured-output/tool checks; Kids moderation stays on OpenAI and fails closed without it |
| Native Hermes Workspace | Configure the model in Hermes separately | The Everyday provider setting does not reconfigure Hermes |

**Local model hosting is not automatically fully offline Homebody.** Use downloaded local model tags, not Ollama cloud models. Ollama also offers `OLLAMA_NO_CLOUD=1` to disable its cloud features; see its [FAQ](https://docs.ollama.com/faq). Nothing here automatically enables a camera, microphone, movement, model download or cloud fallback.

## 1. Install Ollama and download appropriate models

Install the native Ollama application on Windows or macOS. Native Ollama is recommended on Apple Silicon: Docker Desktop on macOS does not pass its GPU through to Ollama. Linux installations work too.

In Terminal or PowerShell:

```text
ollama pull llama3.2:3b
ollama pull qwen2.5vl:3b
ollama list
```

These are starting examples, not universal hardware/quality recommendations. The first is a small tool-capable text model; the second accepts images. You may use other installed models, but text-only models cannot answer camera questions, and not every chat model can call tools or satisfy strict JSON schemas. Match the exact installed tag, including its size/quantization suffix if used. Available RAM/VRAM and other applications determine what fits. CPU inference works but may be too slow for conversation.

Keep Ollama running and the computer awake. Preload a model with `ollama run llama3.2:3b` before a latency check; use `/bye` to exit. Everyday has an existing bounded 20-second text turn, so a cold model load or slow computer can time out. Do not solve that by silently falling back to cloud inference.

## 2. Choose the URL from the caller's point of view

| Caller and Ollama location | URL |
|---|---|
| Both on the same computer | `http://127.0.0.1:11434/v1` |
| Homebody/bridge on another computer | `http://<OLLAMA-COMPUTER-LAN-IP>:11434/v1` |

`127.0.0.1` means **the computer making the request**, not the laptop displaying the dashboard. Camera Ask connects from the robot's Homebody process; Everyday text connects from the bridge. Those two callers can need different URLs. Include `/v1`; do not enter `/api/chat` or `/v1/chat/completions` as the base URL.

### Only when another computer must connect

Ollama listens on loopback by default. Leave that default unchanged for same-computer use. For a separate robot/bridge, bind Ollama to the computer's trusted LAN address (for example `192.168.1.40:11434`), then restart Ollama.

**macOS** (Terminal; replace the example address):

```sh
launchctl setenv OLLAMA_HOST "192.168.1.40:11434"
```

Quit and reopen the Ollama application after setting it. To restore the default, run `launchctl unsetenv OLLAMA_HOST` and reopen Ollama.

**Windows:** Quit Ollama in the system tray. Open **Edit environment variables for your account**, add `OLLAMA_HOST` with value `192.168.1.40:11434` (your own PC's LAN address), then launch Ollama again. Alternatively, PowerShell can save that user variable:

```powershell
[Environment]::SetEnvironmentVariable('OLLAMA_HOST', '192.168.1.40:11434', 'User')
```

Restart Ollama from Start, not an old shell that still has the previous environment. Remove the user variable and restart Ollama to restore loopback-only access.

Allow inbound TCP 11434 **only from the robot and bridge addresses on your trusted private network** in the host firewall. Do not disable the firewall, permit public networks or forward the port on your router. Ollama's local API has no built-in authentication; setting an API-key environment variable in Homebody does not secure the Ollama listener. Prefer a stable/reserved LAN address. Do not expose it directly to the internet. Homebody calls it server-to-server, so wildcard `OLLAMA_ORIGINS` browser access is not needed.

From **each caller**, verify the model list at the chosen address:

```sh
curl http://192.168.1.40:11434/v1/models
```

Windows PowerShell equivalent:

```powershell
Invoke-RestMethod 'http://192.168.1.40:11434/v1/models'
```

A model list proves reachability, not successful generation or tool calling.

## 3. Connect Everyday conversation

On the **existing bridge host**, add these to its environment/configuration, preserving its existing authentication and all other settings:

```dotenv
REACHY_EVERYDAY_PROVIDER=ollama
REACHY_EVERYDAY_MODEL=llama3.2:3b
REACHY_EVERYDAY_URL=http://192.168.1.40:11434/v1
```

Use loopback instead if Ollama runs on the bridge computer. No paid provider key is required for these text calls. The preset uses an ignored placeholder bearer value and never borrows `OPENAI_API_KEY`. Optional `OLLAMA_API_KEY` is solely for an owner-managed authenticated proxy; ordinary local Ollama ignores it.

Restart **only the bridge**, using its existing service/launcher. A `.env` file is not automatically loaded by every launcher: use the same mechanism that already supplies the bridge's settings. Select **Everyday → Pipeline** in Homebody to put conversation reasoning on this provider. A bridge model choice alone does not replace a Realtime/Live speech session.

Check authenticated `/v1/everyday/capabilities`: the provider should be `ollama` with the exact configured model. That is configuration readiness, not model acceptance. Then ask a simple question and a time question, confirming a real answer and time-tool result. Repeat a follow-up and Stop. Keep existing Home Assistant allowlists and camera/movement permissions unchanged.

Advanced: `REACHY_LLM_PROVIDER=ollama`, `REACHY_LLM_URL=...` and an explicit `REACHY_LLM_MODEL=...` select Ollama for shared Agent/Kids/I Spy text calls. `REACHY_AGENT_MODEL`, `REACHY_KIDS_MODEL` and `REACHY_ISPY_MODEL` can override it by purpose. Everyday still needs its explicit `REACHY_EVERYDAY_MODEL`. Do not change these just to enable Everyday: model capabilities and Kids moderation are separate acceptance gates. `tools/llm_provider_check.py` exercises those advanced bridge paths with synthetic prompts; it does not certify Everyday speech or hardware.

## 4. Connect camera vision independently

In Homebody, open **Settings → Local vision and AI acceleration**:

- Enable **Use a local vision model**.
- Set **Vision server URL** to the Ollama URL reachable **from the robot computer**.
- Set **Vision model** to `qwen2.5vl:3b` (or your exact installed vision tag).
- Press **Test vision server**, then save through the normal owner-protected flow.

The server test checks reachability/model listing without taking a photo. To prove inference, explicitly permit AI camera use and ask one camera question when ready; stop/clear afterward. Only the authorized image is sent to the configured server, and the conversation receives its text description. Do not treat a listed model as proof it accepts images. An explicitly configured local vision failure is reported; this setup does not authorize uploading that frame elsewhere.

## Troubleshooting and acceptance

- **Connection refused:** Ollama is stopped, is loopback-only, or the address/port is wrong.
- **Works on the PC but not Reachy:** check the caller's URL, LAN binding and narrowly scoped firewall rule. Merely browsing Homebody from the PC does not make its loopback address reachable to Reachy.
- **Model not found:** compare the exact tag with `ollama list`; download it on the Ollama machine, not on the robot.
- **Tool/JSON errors or no useful answer:** select a model supporting the required tools/structured output; do not assume vision or ordinary chat support implies those capabilities.
- **Timeouts:** try a smaller model, preload it, or check memory pressure with `ollama ps`. Do not run oversized text and vision models together.
- **PC sleeps:** keep it awake for the sessions you intend to use. Homebody does not change its power settings.

Automated coverage includes preset/key isolation, URL selection, request adaptation, a real loopback HTTP tool-result round trip, no fallback on model/server errors and image payload contracts. A portable CI lane is configured to run these contracts on Windows, macOS and Linux without a robot or installed Ollama; results are not established until that workflow actually runs. It does **not** establish native Ollama inference, fresh native Homebody installation, microphone/speaker quality or physical robot acceptance. Complete those separately on the intended Windows/Mac hardware before labelling that installation verified.

Official references: [OpenAI-compatible API](https://docs.ollama.com/api/openai-compatibility), [network/environment configuration](https://docs.ollama.com/faq).
