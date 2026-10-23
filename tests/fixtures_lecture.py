"""
Test fixtures mimicking real lecture frames from ADSE 2509 sessions.
"""

# Fixture 1: Scroll-off scenario (NumericMod.cs)
FRAME_SCROLL_TOP = """\
using System;

namespace NumericFormatDemo
{
    class Program
    {
        static void Main(string[] args)
        {
            decimal decValue = 123456.789m;
            int intValue = 42;
            Console.WriteLine("Currency: {0:C}", decValue);
            Console.WriteLine("Decimal: {0:D}", intValue);
        }
    }
}
"""

FRAME_SCROLL_BOTTOM = """\
            Console.WriteLine("Decimal: {0:D}", intValue);
            Console.WriteLine("Exponential: {0:E}", decValue);
            Console.WriteLine("Fixed-point: {0:F}", decValue);
            Console.WriteLine("General: {0:G}", decValue);
        }
    }
}
"""

# Fixture 2: Tooltip occlusion scenario
FRAME_TOOLTIP_OCCLUDED = """\
            Console.WriteLine("Currency: {0:C}", decValue);
            Console.WriteLine("Dec
"""

FRAME_TOOLTIP_CLEARED = """\
            Console.WriteLine("Currency: {0:C}", decValue);
            Console.WriteLine("Decimal: {0:D}", intValue);
"""

# Fixture 3: Lone-brace anchor scenario (DateTimeSpec.cs)
# Master has the initial class structure ending in braces.
FRAME_LONE_BRACE_MASTER = """\
using System;

namespace sess02_csharp
{
    class DateTimeSpecifiers
    {
        static void Main(string[] args)
        {
            DateTime dt = DateTime.Now;
        }
    }
}
"""

# Lecturer writes new format specifier lines right before the closing braces.
FRAME_LONE_BRACE_NEW = """\
            DateTime dt = DateTime.Now;
            Console.WriteLine("d format: {0:d}", dt);
            Console.WriteLine("D format: {0:D}", dt);
            Console.WriteLine("t format: {0:t}", dt);
        }
    }
}
"""

# Fixture 4: Quote & whitespace variations
FRAME_QUOTES_MASTER = """\
Console.WriteLine("Formatted value: " + total);
"""

FRAME_QUOTES_SINGLE = """\
Console.WriteLine( 'Formatted value: ' + total );
"""
