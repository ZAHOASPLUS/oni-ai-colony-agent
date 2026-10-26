using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection.Metadata;
using System.Reflection.Metadata.Ecma335;
using System.Reflection.PortableExecutable;

class Program
{
    static MetadataReader R;
    static string[] Targets;

    static void Main(string[] args)
    {
        var asmPath = args[0];
        Targets = args.Skip(1).ToArray();
        using var fs = File.OpenRead(asmPath);
        using var pe = new PEReader(fs);
        R = pe.GetMetadataReader();

        foreach (var th in R.TypeDefinitions)
        {
            var td = R.GetTypeDefinition(th);
            var typeName = FullName(td);
            foreach (var mh in td.GetMethods())
            {
                var md = R.GetMethodDefinition(mh);
                if (md.RelativeVirtualAddress == 0) continue;
                var body = pe.GetMethodBody(md.RelativeVirtualAddress);
                var il = body.GetILBytes();
                var callees = ScanCalls(il);
                var hits = callees.Where(c => Targets.Any(t => c.Contains(t, StringComparison.OrdinalIgnoreCase))).ToList();
                if (hits.Count > 0)
                {
                    var name = R.GetString(md.Name);
                    Console.WriteLine($"{typeName}.{name}  ->  {string.Join(" | ", hits.Distinct())}");
                }
            }
        }
    }

    static string FullName(TypeDefinition td)
    {
        var name = R.GetString(td.Name);
        var ns = td.Namespace.IsNil ? "" : R.GetString(td.Namespace);
        return string.IsNullOrEmpty(ns) ? name : ns + "." + name;
    }

    static List<string> ScanCalls(byte[] il)
    {
        var result = new List<string>();
        int i = 0;
        while (i < il.Length)
        {
            byte op = il[i];
            if (op == 0x28 || op == 0x6F || op == 0x73 || op == 0x6E) // call, callvirt, newobj, ldvirtftn
            {
                if (i + 4 < il.Length)
                {
                    int token = BitConverter.ToInt32(il, i + 1);
                    var s = Resolve(token);
                    if (s != null) result.Add(s);
                }
                i += 5;
                continue;
            }
            if (op == 0xFE)
            {
                if (i + 4 < il.Length)
                {
                    byte op2 = il[i + 1];
                    if (op2 == 0x06) // ldftn
                    {
                        int token = BitConverter.ToInt32(il, i + 2);
                        var s = Resolve(token);
                        if (s != null) result.Add(s);
                    }
                }
                i += 4;
                continue;
            }
            i += OpSize(op, il, i);
        }
        return result;
    }

    static int OpSize(byte op, byte[] il, int i)
    {
        switch (op)
        {
            case 0x20: return 5; case 0x21: return 5; case 0x22: return 5; case 0x23: return 5;
            case 0x27: return 9; case 0x29: return 5; case 0x2A: return 1; case 0x2B: return 2;
            case 0x2C: return 2; case 0x2D: return 2; case 0x2E: return 2; case 0x2F: return 2;
            case 0x30: return 2; case 0x31: return 2; case 0x32: return 2; case 0x33: return 2;
            case 0x34: return 2; case 0x35: return 2; case 0x36: return 2; case 0x37: return 2;
            case 0x38: return 2; case 0x39: return 2; case 0x3A: return 2; case 0x3B: return 2;
            case 0x3C: return 2; case 0x3D: return 2; case 0x3E: return 2; case 0x3F: return 2;
            case 0x40: return 2; case 0x41: return 2; case 0x42: return 2; case 0x43: return 2;
            case 0x44: return 2;
            case 0x16: return 1; case 0x15: return 1; case 0x17: return 1; case 0x18: return 1;
            case 0x19: return 1; case 0x1A: return 1; case 0x1B: return 1; case 0x1C: return 1;
            case 0x1D: return 1; case 0x1E: return 1;
            default:
                if (op >= 0x02 && op <= 0x0D) return 1;
                if (op >= 0x0E && op <= 0x13) return 2;
                if (op == 0x11) return 2;
                if (op == 0x12) return 2;
                if (op == 0x13) return 2;
                if (op == 0x14) return 2;
                if (op >= 0x2A && op <= 0x37) return 2;
                if (op >= 0x38 && op <= 0x44) return 2;
                if (op >= 0x45 && op <= 0x57) return 1;
                if (op >= 0x58 && op <= 0x66) return 1;
                if (op >= 0x67 && op <= 0x6E) return 1;
                if (op >= 0x70 && op <= 0x79) return 2;
                if (op == 0x7B) return 5;
                if (op == 0x7C) return 5;
                if (op == 0x7D) return 5;
                if (op == 0x7E) return 5;
                if (op == 0x7F) return 5;
                if (op == 0x80) return 5;
                if (op == 0x81) return 5;
                if (op == 0x82) return 5;
                if (op == 0x83) return 5;
                if (op == 0x84) return 5;
                if (op == 0x85) return 5;
                if (op == 0x86) return 5;
                if (op == 0x8C) return 2; if (op == 0x8D) return 2; if (op == 0x8E) return 2;
                if (op == 0x8F) return 2; if (op == 0x90) return 2; if (op == 0x91) return 2;
                if (op == 0x92) return 2; if (op == 0x93) return 2; if (op == 0x94) return 2;
                if (op == 0x95) return 5; if (op == 0x96) return 5; if (op == 0x97) return 5;
                if (op == 0x98) return 5; if (op == 0x99) return 5; if (op == 0x9A) return 5;
                if (op == 0x9B) return 5; if (op == 0x9C) return 5; if (op == 0x9D) return 5;
                if (op == 0x9E) return 5; if (op == 0x9F) return 5; if (op == 0xA0) return 5;
                if (op == 0xA1) return 5; if (op == 0xA2) return 5; if (op == 0xA3) return 5;
                if (op == 0xA4) return 5; if (op == 0xA5) return 5;
                if (op == 0xB3) return 1; if (op == 0xB4) return 1; if (op == 0xB5) return 1;
                if (op == 0xB6) return 1; if (op == 0xB7) return 1; if (op == 0xB8) return 1;
                if (op == 0xB9) return 1; if (op == 0xBA) return 1; if (op == 0xBB) return 1;
                if (op == 0xBC) return 1; if (op == 0xBD) return 1; if (op == 0xBE) return 1;
                if (op == 0xBF) return 1; if (op == 0xC0) return 1; if (op == 0xC1) return 1;
                if (op == 0xC2) return 1; if (op == 0xC3) return 1; if (op == 0xC4) return 1;
                if (op == 0xC5) return 1; if (op == 0xC6) return 1; if (op == 0xC7) return 1;
                if (op == 0xC8) return 1; if (op == 0xC9) return 1; if (op == 0xCA) return 1;
                if (op == 0xCB) return 1; if (op == 0xCC) return 1; if (op == 0xCD) return 1;
                if (op == 0xCE) return 1; if (op == 0xCF) return 1; if (op == 0xD0) return 1;
                if (op == 0xD1) return 1; if (op == 0xD2) return 1; if (op == 0xD3) return 1;
                if (op == 0xD4) return 1; if (op == 0xD5) return 1; if (op == 0xD6) return 1;
                if (op == 0xD7) return 1; if (op == 0xD8) return 1; if (op == 0xD9) return 1;
                if (op == 0xDA) return 1; if (op == 0xDB) return 1; if (op == 0xDC) return 1;
                if (op == 0xDD) return 1; if (op == 0xDE) return 1; if (op == 0xDF) return 1;
                if (op == 0xE0) return 1;
                if (op == 0xFE) return 2;
                return 1;
        }
    }

    static string Resolve(int token)
    {
        try
        {
            var handle = MetadataTokens.EntityHandle(token);
            switch (handle.Kind)
            {
                case HandleKind.MethodDefinition:
                    var md = R.GetMethodDefinition((MethodDefinitionHandle)handle);
                    var tdh = md.GetDeclaringType();
                    var td = R.GetTypeDefinition(tdh);
                    return FullName(td) + "." + R.GetString(md.Name);
                case HandleKind.MemberReference:
                    var mr = R.GetMemberReference((MemberReferenceHandle)handle);
                    var parent = mr.Parent;
                    string owner = "";
                    if (parent.Kind == HandleKind.TypeReference)
                    {
                        var tr = R.GetTypeReference((TypeReferenceHandle)parent);
                        owner = (tr.Namespace.IsNil ? "" : R.GetString(tr.Namespace) + ".") + R.GetString(tr.Name);
                    }
                    else if (parent.Kind == HandleKind.TypeSpecification)
                    {
                        owner = "typespec";
                    }
                    return owner + "." + R.GetString(mr.Name);
                case HandleKind.MethodSpecification:
                    var ms = R.GetMethodSpecification((MethodSpecificationHandle)handle);
                    return Resolve(MetadataTokens.GetToken(ms.Method));
                default:
                    return null;
            }
        }
        catch { return null; }
    }
}
