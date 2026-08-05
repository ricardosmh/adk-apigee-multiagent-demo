var raw = context.getVariable('response.event.current.content');
context.setVariable('isFinal', false);
try {
  if (raw) {
    var dataLines = [], lines = String(raw).split('\n');
    for (var i = 0; i < lines.length; i++) {
      var t = lines[i].replace(/^\s+/, '');
      if (t.indexOf('data:') === 0) dataLines.push(t.substring(5).replace(/^\s+/, ''));
    }
    var s = dataLines.join('').trim();
    if (s && s !== '[DONE]') {
      var p = JSON.parse(s);
      var inTok, outTok, totalTok, thoughtsTok, fin = false;

      if (p.usageMetadata) {                                   // Gemini
        var gFin = p.candidates && p.candidates[0] && p.candidates[0].finishReason;
        if (gFin) {                                            // only the final chunk
          inTok = p.usageMetadata.promptTokenCount;
          outTok = p.usageMetadata.candidatesTokenCount;
          totalTok = p.usageMetadata.totalTokenCount;
          thoughtsTok = p.usageMetadata.thoughtsTokenCount;
          fin = true;
        }
      } else if (p.type === 'message_start' && p.message && p.message.usage) {
        context.setVariable('anthInput', p.message.usage.input_tokens);   // stash, don't capture
      } else if (p.type === 'message_delta' && p.usage) {      // Anthropic final event
        outTok = p.usage.output_tokens;
        inTok = (p.usage.input_tokens != null) ? p.usage.input_tokens
                                               : context.getVariable('anthInput');
        if (inTok != null && outTok != null) totalTok = Number(inTok) + Number(outTok);
        fin = true;
      }

      if (fin) {
        if (inTok != null)       context.setVariable('inputToken', inTok);
        if (outTok != null)      context.setVariable('outputToken', outTok);
        if (totalTok != null)    context.setVariable('totalToken', totalTok);
        context.setVariable('thoughtsToken', thoughtsTok != null ? thoughtsTok : 0);
        context.setVariable('isFinal', true);
      }
    }
  }
} catch (e) {
  context.setVariable('js_error_occurred', true);
}