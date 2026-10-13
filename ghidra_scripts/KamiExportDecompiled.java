// Writes the decompiled C of the interesting functions to text files, one per function, so they can
// be read side by side, diffed between game versions, or handed to someone (or something) else.
//
// Which functions: every function carrying a bookmark whose category starts with "KAMI" (the other
// KAMI scripts create those), plus the function under the cursor. Optionally the functions they
// call directly are exported as well.
//@author kami-toolkit
//@category KAMI
//@keybinding
//@menupath
//@toolbar

import java.io.File;
import java.io.PrintWriter;
import java.nio.charset.StandardCharsets;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;

import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Bookmark;
import ghidra.program.model.listing.Function;

public class KamiExportDecompiled extends GhidraScript {

	@Override
	protected void run() throws Exception {
		File dir = askDirectory("Folder for the decompiled functions", "Export");
		boolean withCallees = askYesNo("Callees", "Also export the functions they call directly?");

		Map<Address, String> wanted = new LinkedHashMap<>();
		if (currentAddress != null) {
			Function here = getFunctionContaining(currentAddress);
			if (here != null) {
				wanted.put(here.getEntryPoint(), "cursor");
			}
		}
		Iterator<Bookmark> bookmarks = currentProgram.getBookmarkManager().getBookmarksIterator();
		while (bookmarks.hasNext()) {
			Bookmark bookmark = bookmarks.next();
			String category = bookmark.getCategory();
			if (category == null || !category.startsWith("KAMI")) {
				continue;
			}
			Function function = getFunctionContaining(bookmark.getAddress());
			if (function != null) {
				wanted.putIfAbsent(function.getEntryPoint(), category + ": " + bookmark.getComment());
			}
		}
		if (withCallees) {
			Map<Address, String> extra = new LinkedHashMap<>();
			for (Address entry : wanted.keySet()) {
				Function function = getFunctionAt(entry);
				if (function == null) {
					continue;
				}
				for (Function callee : function.getCalledFunctions(monitor)) {
					if (!callee.isThunk() && !callee.isExternal()) {
						extra.putIfAbsent(callee.getEntryPoint(), "called by " + function.getName());
					}
				}
			}
			for (Map.Entry<Address, String> entry : extra.entrySet()) {
				wanted.putIfAbsent(entry.getKey(), entry.getValue());
			}
		}
		if (wanted.isEmpty()) {
			println("KAMI: nothing to export - no KAMI bookmarks and no function under the cursor.");
			return;
		}

		DecompInterface decompiler = new DecompInterface();
		int written = 0;
		int failed = 0;
		try {
			if (!decompiler.openProgram(currentProgram)) {
				printerr("Decompiler could not open the program: " + decompiler.getLastMessage());
				return;
			}
			monitor.initialize(wanted.size());
			for (Map.Entry<Address, String> entry : wanted.entrySet()) {
				monitor.checkCancelled();
				Function function = getFunctionAt(entry.getKey());
				if (function == null) {
					continue;
				}
				monitor.setMessage("KAMI: decompiling " + function.getName());
				DecompileResults results = decompiler.decompileFunction(function, 60, monitor);
				monitor.incrementProgress(1);
				if (results == null || !results.decompileCompleted() || results.getDecompiledFunction() == null) {
					failed++;
					println("could not decompile " + function.getName() + " at " + entry.getKey());
					continue;
				}
				String safeName = function.getName().replaceAll("[^A-Za-z0-9_.-]", "_");
				File out = new File(dir, entry.getKey().toString() + "_" + safeName + ".c");
				try (PrintWriter writer = new PrintWriter(out, StandardCharsets.UTF_8)) {
					writer.println("// " + function.getName() + " at " + entry.getKey() + " in " +
						currentProgram.getName());
					writer.println("// why: " + entry.getValue());
					writer.println();
					writer.print(results.getDecompiledFunction().getC());
				}
				written++;
			}
		}
		finally {
			decompiler.dispose();
		}
		println("KAMI: " + written + " function(s) written to " + dir.getAbsolutePath() +
			(failed > 0 ? ", " + failed + " failed" : ""));
	}
}
