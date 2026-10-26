using System;
using System.Linq;
using System.Reflection;

class Program
{
    static void Main(string[] args)
    {
        var path = args[0];
        var filter = args.Length > 1 ? args[1] : null;
        var asm = Assembly.LoadFrom(path);
        Console.WriteLine("assembly: " + asm.GetName().Name);
        Type[] types;
        try { types = asm.GetTypes(); }
        catch (ReflectionTypeLoadException ex) { types = ex.Types.Where(t => t != null).ToArray(); }
        foreach (var t in types.OrderBy(t => t.FullName))
        {
            if (filter != null && !t.Name.Contains(filter, StringComparison.OrdinalIgnoreCase)) continue;
            Console.WriteLine("TYPE " + t.FullName);
            foreach (var m in t.GetMethods(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
            {
                var ps = string.Join(", ", m.GetParameters().Select(p => p.ParameterType.Name + " " + p.Name));
                Console.WriteLine("   " + (m.IsStatic ? "static " : "") + m.ReturnType.Name + " " + m.Name + "(" + ps + ")");
            }
            foreach (var f in t.GetFields(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly))
            {
                Console.WriteLine("   field " + (f.IsStatic ? "static " : "") + f.FieldType.Name + " " + f.Name);
            }
        }
    }
}
