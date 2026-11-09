using System.Formats.Nrbf;
using System.Globalization;
using System.Text.Json;

const int MaxInput = 8 * 1024 * 1024;
const int MaxOutput = 32 * 1024 * 1024;
const int MaxRecords = 50000;
const int MaxArrayLength = 100000;

try
{
    using var input = Console.OpenStandardInput();
    using var payload = new MemoryStream();
    var chunk = new byte[65536];
    int count;
    while ((count = input.Read(chunk, 0, chunk.Length)) > 0)
    {
        if (payload.Length + count > MaxInput) throw new InvalidDataException("Input size limit exceeded.");
        payload.Write(chunk, 0, count);
    }
    payload.Position = 0;
    if (!NrbfDecoder.StartsWithPayloadHeader(payload)) throw new InvalidDataException("Not an NRBF payload.");
    payload.Position = 0;
    SerializationRecord root = NrbfDecoder.Decode(payload);
    var ids = new Dictionary<SerializationRecord, int>(ReferenceEqualityComparer.Instance);
    var pending = new Queue<SerializationRecord>();
    var records = new Dictionary<string, object?>();
    int memberCount = 0;

    int Register(SerializationRecord record)
    {
        if (ids.TryGetValue(record, out int existing)) return existing;
        if (ids.Count >= MaxRecords) throw new InvalidDataException("Record count limit exceeded.");
        int id = ids.Count;
        ids.Add(record, id);
        pending.Enqueue(record);
        return id;
    }

    object? Value(object? value) => value switch
    {
        null => null,
        PrimitiveTypeRecord primitive => Value(primitive.Value),
        SerializationRecord record => new Dictionary<string, object?> { ["$ref"] = Register(record) },
        DateTime date => date.ToString("O", CultureInfo.InvariantCulture),
        TimeSpan duration => new Dictionary<string, object?> { ["$timespan_ticks"] = duration.Ticks },
        char character => character.ToString(),
        float number when !float.IsFinite(number) => new Dictionary<string, object?> { ["$float"] = number.ToString(CultureInfo.InvariantCulture) },
        double number when !double.IsFinite(number) => new Dictionary<string, object?> { ["$float"] = number.ToString(CultureInfo.InvariantCulture) },
        string or bool or byte or sbyte or short or ushort or int or uint or long or ulong or float or double or decimal => value,
        _ => throw new InvalidDataException("Unsupported primitive representation.")
    };

    object?[] ArrayValues(ArrayRecord record)
    {
        long length = 1;
        foreach (int dimension in record.Lengths)
        {
            if (dimension < 0 || dimension > MaxArrayLength) throw new InvalidDataException("Array length limit exceeded.");
            length = checked(length * dimension);
            if (length > MaxArrayLength) throw new InvalidDataException("Array length limit exceeded.");
        }
        Array array = record switch
        {
            SZArrayRecord<SerializationRecord> r => r.GetArray(),
            SZArrayRecord<ClassRecord> r => r.GetArray(),
            SZArrayRecord<string> r => r.GetArray(),
            SZArrayRecord<bool> r => r.GetArray(),
            SZArrayRecord<byte> r => r.GetArray(),
            SZArrayRecord<sbyte> r => r.GetArray(),
            SZArrayRecord<short> r => r.GetArray(),
            SZArrayRecord<ushort> r => r.GetArray(),
            SZArrayRecord<int> r => r.GetArray(),
            SZArrayRecord<uint> r => r.GetArray(),
            SZArrayRecord<long> r => r.GetArray(),
            SZArrayRecord<ulong> r => r.GetArray(),
            SZArrayRecord<float> r => r.GetArray(),
            SZArrayRecord<double> r => r.GetArray(),
            SZArrayRecord<decimal> r => r.GetArray(),
            SZArrayRecord<char> r => r.GetArray(),
            SZArrayRecord<DateTime> r => r.GetArray(),
            SZArrayRecord<TimeSpan> r => r.GetArray(),
            _ => throw new InvalidDataException("Only single-dimensional NRBF arrays are supported.")
        };
        return array.Cast<object?>().Select(Value).ToArray();
    }

    int rootId = Register(root);
    while (pending.TryDequeue(out SerializationRecord? record))
    {
        var node = new Dictionary<string, object?> { ["type"] = record.TypeName.FullName };
        switch (record)
        {
            case PrimitiveTypeRecord primitive:
                node["kind"] = "primitive";
                node["value"] = Value(primitive.Value);
                break;
            case ClassRecord cls:
                var members = new Dictionary<string, object?>();
                foreach (string name in cls.MemberNames)
                {
                    if (++memberCount > 200000) throw new InvalidDataException("Member count limit exceeded.");
                    members.Add(name, Value(cls.GetRawValue(name)));
                }
                node["kind"] = "class";
                node["members"] = members;
                break;
            case ArrayRecord array:
                node["kind"] = "array";
                node["values"] = ArrayValues(array);
                break;
            default:
                throw new InvalidDataException("Unsupported NRBF record.");
        }
        records.Add(ids[record].ToString(CultureInfo.InvariantCulture), node);
    }

    using var output = new BoundedBuffer(MaxOutput);
    using (var writer = new Utf8JsonWriter(output))
    {
        JsonSerializer.Serialize(writer, new { protocol = 1, root = rootId, records });
    }
    using var stdout = Console.OpenStandardOutput();
    stdout.Write(output.GetBuffer(), 0, checked((int)output.Length));
    stdout.WriteByte(10);
    return 0;
}
catch (Exception exception) when (exception is not OutOfMemoryException and not StackOverflowException)
{
    Console.WriteLine(JsonSerializer.Serialize(new { protocol = 1, error = new { code = "invalid_nrbf", message = exception.Message } }));
    return 2;
}

sealed class BoundedBuffer(int maximum) : MemoryStream
{
    private void Ensure(int count)
    {
        if (Length + count > maximum) throw new InvalidDataException("JSON output size limit exceeded.");
    }
    public override void Write(byte[] buffer, int offset, int count) { Ensure(count); base.Write(buffer, offset, count); }
    public override void Write(ReadOnlySpan<byte> buffer) { Ensure(buffer.Length); base.Write(buffer); }
    public override void WriteByte(byte value) { Ensure(1); base.WriteByte(value); }
}
