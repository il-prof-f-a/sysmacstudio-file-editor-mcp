from zipfile import ZIP_DEFLATED, ZipFile

import pytest


@pytest.fixture
def project_file(tmp_path):
    source = tmp_path / "original.smc2"
    oem = b"""<sysmac schemaVersion="1"><Entity type="Solution" id="s" name="Demo">
<ChildEntities><Entity type="Device" id="cpu" name="Controller"><ChildEntities>
<Entity type="Variables" subtype="Global" id="globals" name="GlobalVariables"/>
<Entity type="Program" subtype="StructuredText" id="p" name="Main"><ChildEntities>
<Entity type="Variables" id="locals" name="Variables"/>
<Entity type="PouBody" subtype="StructuredText" id="body" name="Code"/>
</ChildEntities></Entity>
<Entity type="Program" subtype="MultipartLadder" id="ldp" name="Ladder"><ChildEntities>
<Entity type="PouBody" subtype="Ladder" id="ld" name="Section0"/>
</ChildEntities></Entity>
<Entity type="NexTask" id="task" name="PrimaryTask"/>
</ChildEntities></Entity></ChildEntities></Entity></sysmac>"""
    with ZipFile(source, "w", ZIP_DEFLATED) as archive:
        archive.comment = b"preserve archive comment"
        archive.writestr("s/s.oem", oem)
        archive.writestr("s/s.manifest", '<Manifest><Solution solutionName="Demo"/></Manifest>')
        archive.writestr(
            "s/cpu.xml",
            "<data><ModelName>NX-DEMO</ModelName><ModelDetail>CPU-001</ModelDetail><MajorVersion>2</MajorVersion><MinorVersion>07</MinorVersion></data>",
        )
        archive.writestr(
            "s/globals.xml",
            "[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR_GLOBAL\tGVT=GlobalNamespaceGroup\r\n++D=BOOL\tN=Start\tAT=IOBus://test-unit#0/Synthetic Input 00\tG=VAR_GLOBAL\tUnknown=preserve\r\n++D=BOOL\tN=Run\tIV=FALSE\tG=VAR_GLOBAL\r\n",
        )
        archive.writestr(
            "s/locals.xml",
            "[SLWD version=1.0]\r\n_EN=Variables\r\n+GN=VAR\r\n++D=INT\tN=Counter\tG=VAR\r\n",
        )
        archive.writestr(
            "s/body.xml",
            """<StructuredTextModel xmlns="urn:st"><Text>Run := Start; // Start in comment\nMessage := 'Start';</Text></StructuredTextModel>""",
        )
        archive.writestr(
            "s/ld.xml",
            b'\xef\xbb\xbf{"CLs":[{"__type":"LD","Var":"Start"},{"__type":"ST","Var":"Run"}],"LRI":2,"RRI":3,"VLs":[]}\r\n',
        )
        archive.writestr(
            "s/task.xml",
            '<data><Programs><AssociatedProgramData ProgramName="Main" SequenceNumber="1"/></Programs></data>',
        )
        archive.writestr("s/unrecognised.bin", b"\x00\xffopaque data")
    return source
