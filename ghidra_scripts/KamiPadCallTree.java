// Shows who reads the controller: the callers of cellPadGetData (and its variants), their callers,
// and so on up to a chosen depth. The camera code is somewhere downstream of these functions, so
// they are the natural starting points for following the right stick values through the game.
//
// Import stubs are found by name. Run AnalyzePs3Binary.java (Ps3GhidraScripts) first so that they
// have names; if none is found the script asks for the stub address instead - RPCS3.log prints
// them at boot as lines like:   **** sys_io import: [cellPadGetData] (0x8b72cda1) -> 0x12345678
//
// Every function found gets a bookmark (type Note, category "KAMI pad", depth in the comment).
//@author kami-toolkit
//@category KAMI
//@keybinding
//@menupath
//@toolbar

import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;
import ghidra.program.model.symbol.Reference;

public class KamiPadCallTree extends GhidraScript {

	private static final String[] PAD_FUNCTIONS =
		{ "cellPadGetData", "cellPadGetDataExtra", "cellPadGetRawData", "cellPadPeriphGetData" };

	@Override
	protected void run() throws Exception {
		List<Function> roots = new ArrayList<>();
		FunctionIterator all = currentProgram.getFunctionManager().getFunctions(true);
		while (all.hasNext()) {
			Function function = all.next();
			for (String name : PAD_FUNCTIONS) {
				if (function.getName().equals(name)) {
					roots.add(function);
				}
			}
		}
		if (roots.isEmpty()) {
			Address stub = askAddress("cellPadGetData stub",
				"No function named cellPadGetData was found. Enter the address of its import stub:");
			Function function = getFunctionContaining(stub);
			if (function == null) {
				function = createFunction(stub, "cellPadGetData");
			}
			if (function == null) {
				printerr("No function at " + stub);
				return;
			}
			roots.add(function);
		}
		int maxDepth = askInt("Depth", "How many levels of callers to follow (3 is a good start):");

		Set<Address> seen = new HashSet<>();
		Map<Address, String> level = new LinkedHashMap<>();
		for (Function root : roots) {
			println(root.getName() + " at " + root.getEntryPoint());
			level.put(root.getEntryPoint(), root.getName());
			seen.add(root.getEntryPoint());
		}

		int total = 0;
		for (int depth = 1; depth <= maxDepth && !level.isEmpty(); depth++) {
			Map<Address, String> next = new LinkedHashMap<>();
			for (Map.Entry<Address, String> entry : level.entrySet()) {
				monitor.checkCancelled();
				int dataRefs = 0;
				for (Reference ref : getReferencesTo(entry.getKey())) {
					if (!ref.getReferenceType().isCall()) {
						if (ref.getReferenceType().isData()) {
							dataRefs++;
						}
						continue;
					}
					Function caller = getFunctionContaining(ref.getFromAddress());
					if (caller == null || !seen.add(caller.getEntryPoint())) {
						continue;
					}
					String indent = "  ".repeat(depth);
					println(indent + caller.getEntryPoint() + "  " + caller.getName() + "   (calls " +
						entry.getValue() + " at " + ref.getFromAddress() + ")");
					createBookmark(caller.getEntryPoint(), "KAMI pad",
						"depth " + depth + ": reaches the pad through " + entry.getValue());
					next.put(caller.getEntryPoint(), caller.getName());
					total++;
				}
				if (dataRefs > 0 && depth > 1) {
					println("  ".repeat(depth) + "(" + entry.getValue() + " is also referenced from data " +
						dataRefs + " time(s): it may be called through a pointer or a vtable)");
				}
			}
			level = next;
		}
		println("KAMI: " + total + " function(s) on the way from the pad upwards. Only direct calls are " +
			"followed; a function reached through a pointer shows up as a data reference instead.");
	}
}
