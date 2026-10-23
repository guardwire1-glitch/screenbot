import difflib
from pathlib import Path

master_path = Path("screen_bot/session_data/YouTube_Shadow_Master.java")
master_code = master_path.read_text(encoding="utf-8")

ground_truth = """
public class Main {

    public static void main(String[] args) {

        // method = a block of code that is executed whenever it is called upon
        hello();

    }

    static void hello() {
        System.out.println("Hello");
    }

}


public class Main {

    public static void main(String[] args) {

        // method = a block of code that is executed whenever it is called upon

        int x = 3;
        int y = 4;

        add(x,y);

        System.out.println(z);
    }

    static int add(int x, int y) {

        int z = x + y;
        return z;

    }



}
"""

def clean_lines(text):
    return [l.strip() for l in text.strip().splitlines() if l.strip()]

master_clean = clean_lines(master_code)
gt_clean = clean_lines(ground_truth)

diff = list(difflib.unified_diff(gt_clean, master_clean, fromfile="GroundTruth_Video", tofile="YouTube_Shadow_Master", lineterm=""))
if not diff:
    print("DIFF RESULT: 100% EXACT SUBSTANTIVE MATCH! (0 lines different)")
else:
    print("DIFF RESULT:")
    for line in diff:
        print(line)

print("\n--- GROUND TRUTH LINES (non-blank) ---")
print(f"Total substantive lines: {len(gt_clean)}")
print("\n--- MASTER FILE LINES (non-blank) ---")
print(f"Total substantive lines: {len(master_clean)}")
