// Imports findings produced by kami-toolkit (ps3elf.py --ghidra FILE) into the current program:
// labels, comments and bookmarks, so the hits can be browsed in the listing and the bookmark window.
//
// The file is JSON written by the toolkit, one finding per line inside "items":
//   {"addr": "0x00010224", "label": "", "comment": "field +0x1A8: stfs f0, 0x1A8(r3)", "category": "field"},
// Bookmarks are created under the type "Note" with category "KAMI <category>".
//@author kami-toolkit
//@category KAMI
//@keybinding
//@menupath
//@toolbar

import java.io.File;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;

public class KamiImportFindings extends GhidraScript {

	// The toolkit writes each finding on one line with string values only, so a field can be
	// picked out with a pattern and no JSON library is needed.
	private static String field(String line, String name) {
		Matcher m = Pattern.compile("\"" + name + "\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"").matcher(line);
		if (!m.find()) {
			return "";
		}
		return m.group(1).replace("\\\"", "\"").replace("\\\\", "\\");
	}

	@Override
	protected void run() throws Exception {
		File file = askFile("Findings file written by ps3elf.py --ghidra", "Import");
		List<String> lines = Files.readAllLines(file.toPath(), StandardCharsets.UTF_8);

		int labels = 0;
		int comments = 0;
		int bookmarks = 0;
		int skipped = 0;
		for (String line : lines) {
			monitor.checkCancelled();
			String addrText = field(line, "addr");
			if (addrText.isEmpty()) {
				continue;
			}
			Address addr = toAddr(Long.decode(addrText));
			if (!currentProgram.getMemory().contains(addr)) {
				skipped++;
				continue;
			}
			String label = field(line, "label");
			String comment = field(line, "comment");
			String category = field(line, "category");

			if (!label.isEmpty()) {
				Function function = getFunctionAt(addr);
				if (function != null && function.getSymbol().getSource() == SourceType.DEFAULT) {
					function.setName(label, SourceType.IMPORTED);
				}
				else {
					createLabel(addr, label, false);
				}
				labels++;
			}
			if (!comment.isEmpty()) {
				if (getFunctionAt(addr) != null) {
					String old = getPlateComment(addr);
					setPlateComment(addr, old == null || old.contains(comment) ? comment : old + "\n" + comment);
				}
				else {
					setEOLComment(addr, comment);
				}
				comments++;
			}
			createBookmark(addr, "KAMI " + (category.isEmpty() ? "finding" : category),
				comment.isEmpty() ? label : comment);
			bookmarks++;
		}
		println("KAMI findings imported: " + labels + " labels, " + comments + " comments, " + bookmarks +
			" bookmarks" + (skipped > 0 ? ", " + skipped + " addresses outside this program skipped" : ""));
	}
}
