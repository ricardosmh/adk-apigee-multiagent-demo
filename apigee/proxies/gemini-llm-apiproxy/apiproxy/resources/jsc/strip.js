try {
  var raw = context.getVariable('request.content');
  if (raw) {
    var obj = JSON.parse(raw);
    function strip(parts) {
      if (!parts) return;
      for (var i = 0; i < parts.length; i++) {
        if (parts[i] && typeof parts[i] === 'object') {
          delete parts[i].partMetadata;
          delete parts[i].part_metadata;
        }
      }
    }
    var cs = obj.contents;
    if (cs) {
      if (!Array.isArray(cs)) cs = [cs];
      for (var j = 0; j < cs.length; j++) { if (cs[j]) strip(cs[j].parts); }
    }
    if (obj.systemInstruction) strip(obj.systemInstruction.parts);
    if (obj.system_instruction) strip(obj.system_instruction.parts);
    context.setVariable('request.content', JSON.stringify(obj));
  }
} catch (e) {
  context.setVariable('js_strip_meta_error', String(e));
}