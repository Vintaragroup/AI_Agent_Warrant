# Final Setup: Hold Music Configuration

You now have:
1. ✅ Hold music uploaded to Telnyx Object Storage
2. ✅ Object Storage URL: `https://us-central-1.telnyxcloudstorage.com/hold-music/moonlightdrive.mp3`
3. ✅ FastAPI endpoints: `playback_start` and `playback_stop`
4. ✅ Agent prompt updated to call these endpoints
5. ✅ `requests` library added to requirements

**Next steps:** Configure Render environment and test.

---

## Step 1: Deploy Updated Code to Render

Your code includes:
- `requests` library (just added to requirements.txt)
- `POST /telnyx/playback_start` endpoint
- `POST /telnyx/playback_stop` endpoint
- `GET /telnyx/hold_music_test` diagnostic endpoint

**Action:**
1. Render should auto-deploy when you push (if auto-deploy is enabled)
2. Wait for deployment to complete (watch the Render dashboard)
3. If manual: go to **Render Dashboard** → Your service → **Manual Deploy** → **Deploy latest commit**

---

## Step 2: Set Hold Music URL in Render Environment

1. Go to **Render Dashboard** → Your service (AI_Agent_Warrant)
2. Click **Environment** in the left sidebar
3. Add or update this variable:
   ```
   HOLD_MUSIC_URL=https://us-central-1.telnyxcloudstorage.com/hold-music/moonlightdrive.mp3
   ```
4. Click **Save**
5. **Important:** This will trigger a re-deploy. Wait for it to complete.

---

## Step 3: Test That URL is Accessible

Once deployed, test the hold music URL:

```bash
# Get your Bearer token (TELNYX_TOOL_TOKEN)
export TOKEN="rKEY0198AF24FDB934B96F9A4E90539801E0_JdT29x8sSAEZgK80velwNQ"

# Test 1: Verify endpoint returns the URL
curl -H "Authorization: Bearer $TOKEN" \
  https://ai-agent-warrant.onrender.com/telnyx/hold_music

# Should return:
# {"ok":true,"hold_music_url":"https://us-central-1.telnyxcloudstorage.com/hold-music/moonlightdrive.mp3"}
```

If you get a 403 Forbidden or connection error, the Telnyx Object Storage URL may need a pre-signed URL. See troubleshooting below.

---

## Step 4: Update Agent Configuration in Telnyx

1. Go to **Telnyx Mission Control** → **AI Assistants**
2. Click your assistant (Burt)
3. Replace the **Instructions** field with the content from:
   ```
   docs/Agent_Prompt_Simplified.md
   ```
4. Make sure these **Tools** are configured:
   - `warm_transfer_plan`
   - `playback_start` ← NEW
   - `playback_stop` ← NEW
   - `find_person`
   - `get_bail_status`
   - `attach_caller`
   - Transfer action (workflow step)

5. Click **Save**

---

## Step 5: Test Full Warm Transfer with Hold Music

### Test Sequence:

1. **Place a test call** to your Telnyx number (+17133256085)
2. **Greet caller**, collect inmate info, bail status, caller details
3. **Initiate transfer**:
   - Agent calls `warm_transfer_plan` → gets `hold_music_url`
   - Agent calls `playback_start` with the URL
   - Agent initiates **Voice Transfer** to +16263796590 (your test phone)
4. **Listen on caller's line**: Should hear **moonlightdrive.mp3 playing** (not dead air!)
5. **Your phone rings** with agent whisper
6. **Press 1** to accept transfer
7. **Hold music stops** (agent calls `playback_stop`)
8. **You hear agent** on the line

---

## Troubleshooting

### URL Returns 403 Forbidden

**Problem:** `https://us-central-1.telnyxcloudstorage.com/hold-music/moonlightdrive.mp3` returns HTTP 403

**Solution:** Telnyx Object Storage requires a **pre-signed URL** for direct access. 

**To generate pre-signed URL:**

```bash
export TOKEN="<your-telnyx-api-key>"
export OBJECT_ID="<object-id-from-portal>"

# List objects to find ID if needed
curl -s "https://api.telnyx.com/v2/object_storage/objects" \
  -H "Authorization: Bearer $TOKEN" | jq '.data[] | select(.filename == "moonlightdrive.mp3")'

# Generate pre-signed URL (valid for 7 days)
curl -X POST "https://api.telnyx.com/v2/object_storage/objects/$OBJECT_ID/signed_url" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"expires_in_seconds": 604800}'
```

Response:
```json
{
  "data": {
    "signed_url": "https://telnyx-uploads.s3.amazonaws.com/obj_abc123xyz?Signature=...&Expires=1698453600"
  }
}
```

**Then update Render:**
```
HOLD_MUSIC_URL=https://telnyx-uploads.s3.amazonaws.com/obj_abc123xyz?Signature=...&Expires=1698453600
```

---

### Playback Starts But No Audio Heard

**Problem:** Transfer dials but caller hears silence (no music, no agent)

**Possible causes:**
1. RTP media not flowing (firewall/NAT issue on caller's side)
2. Call disconnects before audio path established
3. Telnyx API error in `playback_start` response

**Debug steps:**
1. Check logs:
   ```bash
   curl -H "Authorization: Bearer $TOKEN" \
     "https://ai-agent-warrant.onrender.com/telnyx/debug_recent?types=telnyx_playback*"
   ```
   Look for `telnyx_playback_start_error` entries.

2. Check Telnyx Mission Control → **Call Logs** for the transfer attempt
   - Look for media error / codec mismatch
   - Check if call was terminated by Telnyx or carrier

3. Verify Agent is actually **calling** `playback_start`
   - Check AI event logs in Telnyx to see tool invocations

---

### Agent Not Calling Playback Tools

**Problem:** Agent doesn't invoke `playback_start` or `playback_stop`

**Solution:**
1. Verify Agent prompt includes explicit tool calls:
   ```
   "Then IMMEDIATELY call playback_start tool with:
    - audio_url: [use hold_music_url from warm_transfer_plan response]
    - loop: true"
   ```
2. Make sure `playback_start` and `playback_stop` tools are **enabled** in Telnyx assistant config
3. Test with a simpler prompt: "Call playback_start with this URL: ..." to force the tool

---

### Render Deployment Fails

If deployment fails after adding `requests` to requirements.txt:

1. Go to **Render Dashboard** → **Events**
2. Look for build error messages
3. Common issues:
   - `requests` not found: Wait 30 seconds, then manually redeploy
   - Package conflict: Try reinstalling build
4. If stuck: Contact Render support or check GitHub Actions logs

---

## Next Steps

1. **Deploy** updated code to Render (should be automatic)
2. **Set `HOLD_MUSIC_URL`** in Render environment
3. **Test endpoint**: `GET /telnyx/hold_music_test`
4. **Update Agent prompt** in Telnyx with `docs/Agent_Prompt_Simplified.md`
5. **Place test call** and verify hold music plays

---

## Expected Outcome

✅ **Caller hears hold music while waiting for agent to answer**
✅ **Agent hears whisper text and can press 1 to accept**
✅ **Music stops when agent answers**
✅ **Smooth handoff between AI and human agent**

---

## References

- Playback Start API: https://developers.telnyx.com/docs/api/v2/call-control/call-commands#playbackStart
- Playback Stop API: https://developers.telnyx.com/docs/api/v2/call-control/call-commands#playbackStop
- Object Storage: https://developers.telnyx.com/docs/object-storage
