// Finds code that reads or writes a struct field at given displacements, e.g. the yaw and pitch
// offsets of the camera object found at run time (the last offsets of the pointer chain).
//
// Official RPCS3 builds cannot break on a memory write, so this is the static way to answer
// "which function updates the camera angles": every lfs/stfs (optionally other loads and stores)
// whose displacement is one of the offsets is collected, grouped by function, and the functions
// are ranked by how many of the offsets they touch and how many of the accesses are stores.
// The best functions get a bookmark (type Note, category "KAMI field") and the hits a comment.
//
// Run it after auto-analysis. Works on any PowerPC program; nothing PS3-specific is assumed.
//@author kami-toolkit
//@category KAMI
//@keybinding
//@menupath
//@toolbar

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;

import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.lang.Register;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.scalar.Scalar;

public class KamiFindFieldAccess extends GhidraScript {

	private static class Hit {
		Address address;
		long offset;
		boolean store;
		String text;
	}

	private static class Group {
		Address entry;
		String name;
		List<Hit> hits = new ArrayList<>();
		Set<Long> offsets = new TreeSet<>();
		int stores;
	}

	@Override
	protected void run() throws Exception {
		String offsetText = askString("Field offsets",
			"Displacements to look for, comma separated (e.g. 0x1A8,0x1C4):", "0x1A8,0x1C4");
		String opText = askString("Instructions",
			"Mnemonics to match, comma separated:", "lfs,stfs,lfsu,stfsu");
		int minOffsets = askInt("Minimum match", "A function must touch at least this many of the offsets:");

		Set<Long> wanted = new HashSet<>();
		for (String part : offsetText.split(",")) {
			if (!part.trim().isEmpty()) {
				wanted.add(Long.decode(part.trim()));
			}
		}
		Set<String> mnemonics = new HashSet<>();
		for (String part : opText.split(",")) {
			if (!part.trim().isEmpty()) {
				mnemonics.add(part.trim().toLowerCase());
			}
		}

		Map<Address, Group> groups = new HashMap<>();
		InstructionIterator it = currentProgram.getListing().getInstructions(true);
		long scanned = 0;
		while (it.hasNext()) {
			Instruction ins = it.next();
			if ((++scanned & 0xFFFF) == 0) {
				monitor.checkCancelled();
				monitor.setMessage("KAMI: scanned " + scanned + " instructions");
			}
			String mnemonic = ins.getMnemonicString().toLowerCase();
			if (!mnemonics.contains(mnemonic)) {
				continue;
			}
			// Operand 1 of a D-form load/store is "disp(base)": a scalar and a register.
			Long displacement = null;
			String base = null;
			for (int op = 0; op < ins.getNumOperands(); op++) {
				Object[] objects = ins.getOpObjects(op);
				if (objects.length < 2) {
					continue;
				}
				for (Object o : objects) {
					if (o instanceof Scalar) {
						displacement = ((Scalar) o).getSignedValue();
					}
					else if (o instanceof Register) {
						base = ((Register) o).getName();
					}
				}
			}
			if (displacement == null || base == null || !wanted.contains(displacement)) {
				continue;
			}
			// r1 is the stack frame, r2 the TOC, r13 small data: not fields of an object.
			if (base.equals("r1") || base.equals("r2") || base.equals("r13")) {
				continue;
			}

			Function function = getFunctionContaining(ins.getAddress());
			Address key = function != null ? function.getEntryPoint() : ins.getAddress();
			Group group = groups.get(key);
			if (group == null) {
				group = new Group();
				group.entry = key;
				group.name = function != null ? function.getName() : "(no function)";
				groups.put(key, group);
			}
			Hit hit = new Hit();
			hit.address = ins.getAddress();
			hit.offset = displacement;
			hit.store = mnemonic.startsWith("st");
			hit.text = ins.toString();
			group.hits.add(hit);
			group.offsets.add(displacement);
			if (hit.store) {
				group.stores++;
			}
		}

		List<Group> ranked = new ArrayList<>(groups.values());
		ranked.sort((a, b) -> {
			if (a.offsets.size() != b.offsets.size()) {
				return b.offsets.size() - a.offsets.size();
			}
			if (a.stores != b.stores) {
				return b.stores - a.stores;
			}
			return a.entry.compareTo(b.entry);
		});

		int shown = 0;
		for (Group group : ranked) {
			if (group.offsets.size() < minOffsets) {
				continue;
			}
			StringBuilder offsets = new StringBuilder();
			for (Long offset : group.offsets) {
				offsets.append(offsets.length() > 0 ? ", " : "").append("0x").append(Long.toHexString(offset));
			}
			println(String.format("%s  %s : offsets %d/%d (%s), accesses %d, stores %d", group.entry,
				group.name, group.offsets.size(), wanted.size(), offsets, group.hits.size(), group.stores));
			for (Hit hit : group.hits) {
				println("      " + hit.address + "  " + hit.text);
				setEOLComment(hit.address, "KAMI field +0x" + Long.toHexString(hit.offset) +
					(hit.store ? " (store)" : " (load)"));
			}
			createBookmark(group.entry, "KAMI field", "touches " + offsets + ", " + group.stores + " stores");
			shown++;
			if (shown >= 60) {
				println("... more functions matched; raise the minimum match to narrow the list");
				break;
			}
		}
		println("KAMI: " + shown + " function(s) listed out of " + groups.size() +
			" touching any of the offsets. Functions that STORE to every offset are the first candidates " +
			"for the camera update code.");
	}
}
