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
    static PEReader PE;

    static void Main(string[] args)
    {
        var asmPath = args[0];
        var typeFilter = args[1];
        var methodFilter = args.Length > 2 ? args[2] : null;

        using var fs = File.OpenRead(asmPath);
        PE = new PEReader(fs);
        R = PE.GetMetadataReader();

        foreach (var th in R.TypeDefinitions)
        {
            var td = R.GetTypeDefinition(th);
            var typeName = FullName(td);
            if (!typeName.Contains(typeFilter, StringComparison.OrdinalIgnoreCase)) continue;
            foreach (var mh in td.GetMethods())
            {
                var md = R.GetMethodDefinition(mh);
                var name = R.GetString(md.Name);
                if (methodFilter != null && !name.Contains(methodFilter, StringComparison.OrdinalIgnoreCase)) continue;
                if (md.RelativeVirtualAddress == 0) continue;
                var ps = string.Join(", ", md.GetParameters().Select(p => R.GetString(R.GetParameter(p).Name)));
                Console.WriteLine($"### {typeName}.{name}({ps})");
                Dump(md.RelativeVirtualAddress);
                Console.WriteLine();
            }
        }
    }

    static void Dump(int rva)
    {
        var body = PE.GetMethodBody(rva);
        var il = body.GetILBytes();
        int i = 0;
        while (i < il.Length)
        {
            int start = i;
            byte op = il[i++];
            string opName = OpName(op);
            string operand = "";
            if (op == 0xFE && i < il.Length)
            {
                byte op2 = il[i++];
                opName = OpName2(op2);
                if (op2 == 0x06) { if (i + 4 <= il.Length) { operand = Resolve(BitConverter.ToInt32(il, i)); i += 4; } }
                else if (op2 == 0x09 || op2 == 0x0A || op2 == 0x0B || op2 == 0x0C || op2 == 0x0D || op2 == 0x0E) { i += 4; }
                continue;
            }
            switch (op)
            {
                case 0x28: case 0x6F: case 0x73: case 0x6E:
                    if (i + 4 <= il.Length) { operand = Resolve(BitConverter.ToInt32(il, i)); i += 4; }
                    break;
                case 0x72:
                    if (i + 4 <= il.Length) { operand = "str:" + StringAt(BitConverter.ToInt32(il, i)); i += 4; }
                    break;
                case 0x7B: case 0x7C: case 0x7D: case 0x7E: case 0x7F: case 0x80: case 0x81: case 0x82:
                case 0x83: case 0x84: case 0x85: case 0x86:
                    if (i + 4 <= il.Length) { operand = FieldAt(BitConverter.ToInt32(il, i)); i += 4; }
                    break;
                case 0x20:
                    if (i + 4 <= il.Length) { operand = "i4:" + BitConverter.ToInt32(il, i); i += 4; }
                    break;
                case 0x8D: case 0x8C: case 0x8E:
                    if (i + 4 <= il.Length) { operand = "type:" + TypeAt(BitConverter.ToInt32(il, i)); i += 4; }
                    break;
                case 0x0E: case 0x11: case 0x12: case 0x13: case 0x14:
                    i += 1;
                    break;
                case 0x2B: case 0x2C: case 0x2D: case 0x2E: case 0x2F: case 0x30: case 0x31: case 0x32:
                case 0x33: case 0x34: case 0x35: case 0x36: case 0x37: case 0x38: case 0x39: case 0x3A:
                case 0x3B: case 0x3C: case 0x3D: case 0x3E: case 0x3F: case 0x40: case 0x41: case 0x42:
                case 0x43: case 0x44:
                    i += 1;
                    break;
                case 0x45:
                    i += 4;
                    break;
                default:
                    break;
            }
            if (operand.Length > 0) Console.WriteLine($"   {start:X4}: {opName} {operand}");
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
                    return FullName(R.GetTypeDefinition(md.GetDeclaringType())) + "." + R.GetString(md.Name);
                case HandleKind.MemberReference:
                    var mr = R.GetMemberReference((MemberReferenceHandle)handle);
                    string owner = "";
                    if (mr.Parent.Kind == HandleKind.TypeReference)
                    {
                        var tr = R.GetTypeReference((TypeReferenceHandle)mr.Parent);
                        owner = (tr.Namespace.IsNil ? "" : R.GetString(tr.Namespace) + ".") + R.GetString(tr.Name);
                    }
                    return owner + "." + R.GetString(mr.Name);
                case HandleKind.MethodSpecification:
                    var ms = R.GetMethodSpecification((MethodSpecificationHandle)handle);
                    return Resolve(MetadataTokens.GetToken(ms.Method));
                default:
                    return "";
            }
        }
        catch { return ""; }
    }

    static string FieldAt(int token)
    {
        try
        {
            var handle = MetadataTokens.EntityHandle(token);
            if (handle.Kind == HandleKind.FieldDefinition)
            {
                var fd = R.GetFieldDefinition((FieldDefinitionHandle)handle);
                return FullName(R.GetTypeDefinition(fd.GetDeclaringType())) + "." + R.GetString(fd.Name);
            }
            if (handle.Kind == HandleKind.MemberReference)
            {
                var mr = R.GetMemberReference((MemberReferenceHandle)handle);
                string owner = "";
                if (mr.Parent.Kind == HandleKind.TypeReference)
                {
                    var tr = R.GetTypeReference((TypeReferenceHandle)mr.Parent);
                    owner = (tr.Namespace.IsNil ? "" : R.GetString(tr.Namespace) + ".") + R.GetString(tr.Name);
                }
                return owner + "." + R.GetString(mr.Name);
            }
        }
        catch { }
        return "";
    }

    static string TypeAt(int token)
    {
        try
        {
            var handle = MetadataTokens.EntityHandle(token);
            if (handle.Kind == HandleKind.TypeReference)
            {
                var tr = R.GetTypeReference((TypeReferenceHandle)handle);
                return (tr.Namespace.IsNil ? "" : R.GetString(tr.Namespace) + ".") + R.GetString(tr.Name);
            }
            if (handle.Kind == HandleKind.TypeDefinition)
            {
                return FullName(R.GetTypeDefinition((TypeDefinitionHandle)handle));
            }
        }
        catch { }
        return "";
    }

    static string StringAt(int token)
    {
        try
        {
            var handle = MetadataTokens.UserStringHandle(token);
            return R.GetUserString(handle);
        }
        catch { return ""; }
    }

    static string FullName(TypeDefinition td)
    {
        var name = R.GetString(td.Name);
        var ns = td.Namespace.IsNil ? "" : R.GetString(td.Namespace);
        return string.IsNullOrEmpty(ns) ? name : ns + "." + name;
    }

    static string OpName(byte op) => op switch
    {
        0x28 => "call", 0x6F => "callvirt", 0x73 => "newobj", 0x6E => "ldvirtftn",
        0x72 => "ldstr", 0x7B => "ldfld", 0x7C => "ldflda", 0x7D => "stfld",
        0x7E => "ldsfld", 0x80 => "stsfld", 0x20 => "ldc.i4", 0x8D => "newarr",
        0x8C => "box", 0x8E => "ldtoken", 0x2A => "ret", 0xFE => "prefix",
        _ => "op_" + op.ToString("X2")
    };

    static string OpName2(byte op2) => op2 switch
    {
        0x06 => "ldftn", 0x09 => "ldarg", 0x0A => "ldarga", 0x0B => "starg",
        0x0C => "ldloc", 0x0D => "ldloca", 0x0E => "stloc", 0x15 => "initobj",
        _ => "op2_" + op2.ToString("X2")
    };
}
