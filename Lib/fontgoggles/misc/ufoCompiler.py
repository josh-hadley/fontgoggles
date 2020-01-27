from contextlib import redirect_stdout, redirect_stderr
import io
import logging
import re
import shlex
import sys
import traceback
import xml.etree.ElementTree as ET
from fontTools.fontBuilder import FontBuilder
from fontTools.ufoLib import UFOReader
from fontTools.ufoLib.glifLib import _BaseParser as BaseGlifParser
from ufo2ft.featureCompiler import FeatureCompiler

#
# Tools to compile a UFO's features as quickly as possible.
#


def compileMinimumFont_captureOutput(ufoPath):
    f = io.StringIO()
    with redirect_stdout(f), redirect_stderr(f):
        try:
            ttFont, error = compileMinimumFont(ufoPath)
        except Exception:
            data = None
            error = traceback.format_exc()
        else:
            f = io.BytesIO()
            ttFont.save(f, reorderTables=False)
            data = f.getvalue()
    return data, f.getvalue(), error


def compileMinimumFont(ufoPath):
    """Compile the source UFO to a TTF with the smallest amount of tables
    needed to let HarfBuzz do its work. That would be 'cmap', 'post' and
    whatever OTL tables are needed for the features. Return the compiled
    font data.

    This function may do some redundant work (eg. we need an UFOReader
    elsewhere, too), but having a picklable argument and return value
    allows us to run it in a separate process, enabling parallelism.
    """
    reader = UFOReader(ufoPath, validate=False)
    glyphSet = reader.getGlyphSet()
    info = UFOInfo()
    reader.readInfo(info)

    glyphOrder = sorted(glyphSet.keys())  # no need for the "real" glyph order
    if ".notdef" not in glyphOrder:
        # We need a .notdef glyph, so let's make one.
        glyphOrder.insert(0, ".notdef")
    cmap, revCmap, anchors = fetchCharacterMappingAndAnchors(glyphSet, ufoPath)
    fb = FontBuilder(round(info.unitsPerEm))
    fb.setupGlyphOrder(glyphOrder)
    fb.setupCharacterMap(cmap)
    fb.setupPost()  # This makes sure we store the glyph names
    ttFont = fb.font
    ufo = MinimalFontObject(ufoPath, reader, revCmap, anchors)
    feaComp = FeatureCompiler(ufo, ttFont)
    try:
        feaComp.compile()
    except Exception:
        error = traceback.format_exc()
    else:
        error = None
    return ttFont, error


class UFOInfo:
    pass


_unicodeOrAnchorGLIFPattern = re.compile(re.compile(rb'(<\s*(anchor|unicode)\s+([^>]+)>)'))
_unicodeAttributeGLIFPattern = re.compile(re.compile(rb'hex\s*=\s*\"([0-9A-Fa-f]+)\"'))


def fetchCharacterMappingAndAnchors(glyphSet, ufoPath):
    # This seems about 2.3 times faster than reader.getCharacterMapping()
    cmap = {}  # unicode: glyphName
    revCmap = {}
    anchors = {}  # glyphName: [(anchorName, x, y), ...]
    duplicateUnicodes = set()
    for glyphName in sorted(glyphSet.keys()):
        data = glyphSet.getGLIF(glyphName)
        if b"<!--" in data:
            # Fall back to proper parser, assuming this to be uncommon
            unicodes, glyphAnchors = fetchUnicodesAndAnchors(data)
        else:
            # Fast route with regex
            unicodes = []
            glyphAnchors = []
            for rawElement, tag, rawAttributes in _unicodeOrAnchorGLIFPattern.findall(data):
                if tag == b"unicode":
                    m = _unicodeAttributeGLIFPattern.match(rawAttributes)
                    try:
                        unicodes.append(int(m.group(1), 16))
                    except ValueError:
                        pass
                elif tag == b"anchor":
                    root = ET.fromstring(rawElement)
                    glyphAnchors.append(_parseAnchorAttrs(root.attrib))
        uniqueUnicodes = []
        for codePoint in unicodes:
            if codePoint not in cmap:
                cmap[codePoint] = glyphName
                uniqueUnicodes.append(codePoint)
            else:
                duplicateUnicodes.add(codePoint)
        if glyphAnchors:
            anchors[glyphName] = glyphAnchors
        if uniqueUnicodes:
            revCmap[glyphName] = uniqueUnicodes

    if duplicateUnicodes:
        logger = logging.getLogger("fontgoggles.font.ufoFont")
        logger.warning("Some code points in '%s' are assigned to multiple glyphs: %s", ufoPath, sorted(duplicateUnicodes))
    return cmap, revCmap, anchors


def fetchUnicodesAndAnchors(glif):
    """
    Get a list of unicodes listed in glif.
    """
    parser = FetchUnicodesAndAnchorsParser()
    parser.parse(glif)
    return parser.unicodes, parser.anchors


def _parseNumber(s):
    if not s:
        return None
    f = float(s)
    i = int(f)
    if i == f:
        return i
    return f


def _parseAnchorAttrs(attrs):
    return attrs.get("name"), _parseNumber(attrs.get("x")), _parseNumber(attrs.get("y"))


class FetchUnicodesAndAnchorsParser(BaseGlifParser):

    def __init__(self):
        self.unicodes = []
        self.anchors = []
        super().__init__()

    def startElementHandler(self, name, attrs):
        if self._elementStack and self._elementStack[-1] == "glyph":
            if name == "unicode":
                value = attrs.get("hex")
                if value is not None:
                    try:
                        value = int(value, 16)
                        if value not in self.unicodes:
                            self.unicodes.append(value)
                    except ValueError:
                        pass
            elif name == "anchor":
                self.anchors.append(_parseAnchorAttrs(attrs))
        super().startElementHandler(name, attrs)


class MinimalFontObject:

    # This class and its relatives implement a defcon-like font object, but
    # only support the bare minimum for ufo2ft's FeatureCompiler to do its
    # work. No outlines are needed, no advances, no glyph.lib, only glyph
    # unicodes and anchors, and at the font level, only features, groups,
    # kerning and lib are needed.

    def __init__(self, ufoPath, reader, revCmap, anchors):
        self.path = ufoPath
        self._revCmap = revCmap
        self._anchors = anchors
        self._glyphNames = set(reader.getGlyphSet().contents.keys())
        self._glyphNames.add(".notdef")  # ensure we have .notdef
        self.features = MinimalFeaturesObject(reader.readFeatures())
        self.groups = reader.readGroups()
        self.kerning = reader.readKerning()
        self.lib = reader.readLib()
        self._glyphs = {}

    def keys(self):
        return self._glyphNames

    def __getitem__(self, glyphName):
        if glyphName not in self._glyphNames:
            raise KeyError(glyphName)
        # TODO: should we even bother caching?
        glyph = self._glyphs.get(glyphName)
        if glyph is None:
            glyph = MinimalGlyphObject(glyphName, self._revCmap.get(glyphName), self._anchors.get(glyphName, ()))
            self._glyphs[glyphName] = glyphName
        return glyph


class MinimalGlyphObject:

    def __init__(self, name, unicodes, anchors):
        self.name = name
        self.unicodes = unicodes
        self.anchors = [MinimalAnchorObject(name, x, y) for name, x, y in anchors]

    @property
    def unicode(self):
        return self.unicodes[0] if self.unicodes else None


class MinimalAnchorObject:

    def __init__(self, name, x, y):
        self.name = name
        self.x = x
        self.y = y


class MinimalFeaturesObject:

    def __init__(self, featureText):
        self.text = featureText


ERROR_MARKER = "---- ERROR ----"
SUCCESS_MARKER = "---- SUCCESS ----"


def ufoCompileServer():
    while True:
        input = sys.stdin.readline()
        input = input.strip()
        if not input:
            break
        try:
            ufoPath, ttPath = shlex.split(input)
            ttFont, error = compileMinimumFont(ufoPath)
            if error:
                print(error)
            ttFont.save(ttPath, reorderTables=False)
        except:
            traceback.print_exc()
            print(ERROR_MARKER)
        else:
            print(SUCCESS_MARKER)


if __name__ == "__main__":
    ufoCompileServer()
