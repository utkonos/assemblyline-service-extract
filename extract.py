from __future__ import absolute_import

import email
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import zlib
from copy import deepcopy

from assemblyline.common.reaper import set_death_signal
from assemblyline.common.timeout import SubprocessTimer
from bs4 import BeautifulSoup
from lxml import html, etree

from assemblyline.common.identify import ident
from assemblyline.common.str_utils import safe_str
from assemblyline_v4_service.common.base import ServiceBase
from assemblyline_v4_service.common.result import Result, ResultSection
from assemblyline_v4_service.common.request import ServiceRequest
from .doc_extract import mstools, extract_docx, ExtractionError, PasswordError
from .ext.xxxswf import xxxswf
from .repair_zip import RepairZip, BadZipfile

extract_docx = None
msoffice = None
RepairZip = None
BadZipfile = None
ExtractionError = None
BeautifulSoup = None
mstools = None

chunk_size = 65536
DEBUG = False


class ExtractMaxExceeded(Exception):
    pass


class ExtractIgnored(Exception):
    pass


class Extract(ServiceBase):
    FORBIDDEN_EXE = [".text", ".rsrc", ".rdata", ".reloc", ".pdata", ".idata", "UPX", "file"]
    FORBIDDEN_ELF_EXE = [str(x) for x in range(20)]
    MAX_EXTRACT = 500
    MAX_EXTRACT_LIVE = 100

    LAUNCHABLE_EXTENSIONS = [
        '.ade',
        '.adp',
        '.as',   # Adobe ActionScript
        '.bat',  # DOS/Windows batch file
        '.chm',
        '.cmd',  # Windows command
        '.com',  # DOS command
        '.cpl',
        '.exe',  # DOS/Windows executable
        '.dll',  # Windows library
        '.hta',
        '.inf',  # Windows autorun file
        '.ins',
        '.isp',
        '.jar',  # Java JAR
        '.jse',
        '.js',   # Javascript
        '.lib',
        '.lnk',  # Windows shortcut
        '.mde',
        '.msc',
        '.msp',
        '.mst',
        '.pif',
        '.py',   # Python script
        '.scr',  # Windows screen saver
        '.sct',
        '.shb',
        '.sys',
        '.vb',   # VB Script
        '.vbe',  # Encrypted VB script
        '.vbs',  # VB Script
        '.vxd',
        '.wsc',
        '.wsf',
        '.wsh'
    ]

    def __init__(self, config=None):
        super(Extract, self).__init__(config)
        self._last_password = None
        self.extract_methods = [
            self.extract_7zip,
            self.extract_tnef,
            self.extract_swf,
            self.extract_ace,
            self.extract_eml,
            self.repair_zip,
            self.extract_office,
            self.extract_pdf,
            self.extract_vbe,
        ]
        self.anomaly_detections = [self.archive_with_executables, self.archive_is_arc]
        self.white_listing_methods = [self.jar_whitelisting]
        self.st = None
        self.named_attachments_only = None
        self.max_attachment_size = None
        self.is_ipa = False
        self.sha = None

    def start(self):
        self.st = SubprocessTimer(2*self.SERVICE_TIMEOUT/3)
        self.named_attachments_only = self.config.get('NAMED_EMAIL_ATTACHMENTS_ONLY', True)
        self.max_attachment_size = self.config.get('MAX_EMAIL_ATTACHMENT_SIZE', None)

    def execute(self, request):
        """Main Module. See README for details."""
        result = Result()
        self.sha = request.sha256
        continue_after_extract = request.get_param('continue_after_extract')
        self._last_password = None
        self.is_ipa = False
        local = request.file_path
        password_protected = False
        white_listed = 0

        # Add warning as new module requires change to service configuration
        if "pdf" not in request._ser.SERVICE_ACCEPTS:
            self.log.warning('Extract service cannot run PDF module due to service configuration. Add "document/pdf" to'
                             'SERVICE_ACCEPTS option to enable')

        try:
            password_protected, white_listed = self.extract(request, local)
        except ExtractMaxExceeded as e:
            result.add_section(ResultSection(str(e)))
        except ExtractIgnored as e:
            result.add_section(ResultSection(str(e)))
        except ExtractionError as ee:
            # If we don't support the encryption method. This will tell us what we need to add support for
            result.add_section(
                ResultSection(score=SCORE.VHIGH,
                              title_text="Password protected file, could not extract: %s" % ee.message)
            )

        if request.extracted:
            if request.tag.startswith("executable"):
                result.report_heuristic(Extract.AL_EXTRACT_002)
            elif request.tag.startswith("java"):
                result.report_heuristic(Extract.AL_EXTRACT_003)
            elif request.tag.startswith("android"):
                result.report_heuristic(Extract.AL_EXTRACT_004)
            elif request.tag.startswith("document/email"):
                result.report_heuristic(Extract.AL_EXTRACT_005)
            elif request.tag.startswith("document/office"):
                result.report_heuristic(Extract.AL_EXTRACT_006)
            elif request.tag.startswith("document/pdf"):
                result.report_heuristic(Extract.AL_EXTRACT_007)
            elif request.tag.startswith("archive/audiovisual/flash"):
                result.report_heuristic(Extract.AL_EXTRACT_008)
            elif request.tag.startswith("code/vbe"):
                result.report_heuristic(Extract.AL_EXTRACT_011)
            elif self.is_ipa:
                result.report_heuristic(Extract.AL_EXTRACT_009)
            else:
                result.report_heuristic(Extract.AL_EXTRACT_001)
            # Only password protected office documents are extracted by service, so no need to add an extra heuristic
            if password_protected and not request.tag.startswith("document/office"):
                result.report_heuristic(Extract.AL_EXTRACT_010)

        num_extracted = len(request.extracted)

        section = None
        if not request.extracted and password_protected:
            section = ResultSection(SCORE.VHIGH,
                                    "Failed to extract password protected file.")
            section.add_tag('file.behavior', "Archive Unknown Password", TAG_WEIGHT['MED'])

        elif request.extracted and password_protected and self._last_password is not None:
            section = ResultSection(f"Successfully extracted {num_extracted} file{'s' if num_extracted > 0 else ''} "
                                    f"using password: {self._last_password}")

        elif request.extracted and password_protected and self._last_password is None:
            pw_list = " | ".join(self.get_passwords(request))
            section = ResultSection(f"Successfully extracted {num_extracted} file{'s' if num_extracted > 0 else ''} "
                                    f"using one of the following passwords: {pw_list}")

        elif num_extracted != 0 and white_listed != 0:
            section = ResultSection(f"Successfully extracted {num_extracted} file{'s' if num_extracted > 0 else ''} "
                                    f"out of {white_listed + num_extracted}. The other {white_listed} "
                                    f"file{'s' if white_listed > 0 else ''} were whitelisted")

        elif num_extracted != 0:
            section = ResultSection(f"Successfully extracted {num_extracted} file{'s' if num_extracted > 0 else ''}")

        if section:
            result.add_section(section)

        for anomaly in self.anomaly_detections:
            anomaly(request, result)

        if (request.extracted
                and not request.file_type.startswith("executable")
                and not request.file_type.startswith("java")
                and not request.file_type.startswith("android")
                and not request.file_type.startswith("document")
                and not self.is_ipa
                and not continue_after_extract) \
                or (request.file_type == "document/email"
                    and not continue_after_extract):
            request.drop()

        request.result = result

    def extract(self, request, local):
        """Iterate through extraction methods to extract archived, embedded or encrypted content from a sample.

        Args:
            request: AL request object.
            local: File path of AL sample.

        Returns:
            True if archive is password protected, and number of white-listed embedded files.
        """
        encoding = request.tag.replace('archive/', '')
        password_protected = False
        white_listed_count = 0
        extracted = []

        # Try all extracting methods
        for extract_method in self.extract_methods:
            extracted, temp_password_protected = extract_method(request, local, encoding)
            if temp_password_protected:
                password_protected = temp_password_protected
            if extracted:
                break

        # Perform needed white listing
        if extracted:
            for white_listing_method in self.white_listing_methods:
                extracted, white_listed_count = white_listing_method(extracted, white_listed_count, encoding)

        for child in extracted:
            if not request.add_extracted(*child):
                raise ExtractMaxExceeded("This file contains more files than the maximum embedded number of %s. "
                                         "None of the %s files where extracted."
                                         % (str(request.max_extracted), str(len(extracted))))

        return password_protected, white_listed_count

    def get_passwords(self, request: ServiceRequest):
        """Create list of possible password strings to be used against AL sample if encryption is detected.
        Uses service configuration variable 'DEFAULT_PW_LIST'; submission parameter 'password' (if supplied); and
        content of email body (if 'email_body' is in submission tags).

        Args:
            request: AL request object.

        Returns:
            List of strings.
        """
        passwords = deepcopy(self.cfg.get('DEFAULT_PW_LIST', []))
        user_supplied = request.get_param('password')
        if user_supplied:
            passwords.append(user_supplied)

        if "email_body" in self.submission_tags:
            passwords.extend(self.submission_tags["email_body"])

        return passwords

    # noinspection PyCallingNonCallable
    def repair_zip(self, _, local, encoding):
        """Attempts to use modules in repair_zip.py when a possible corruption of ZIP archive has been detected.

        Args:
             _: Unused AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing repaired zip path, encoding, and display name "repaired_zip_file.zip", or a blank list if
            repair failed; and False as encryption will not be detected.
        """
        try:
            with RepairZip(local, strict=False) as rz:
                if not (rz.is_zip and rz.broken):
                    return [], False
                rz.fix_zip()

                with tempfile.NamedTemporaryFile(dir=self.working_directory, delete=False) as fh:
                    out_name = fh.name
                    with RepairZip(fh, "w") as zo:
                        for path in rz.namelist():
                            with tempfile.NamedTemporaryFile(dir=self.working_directory, delete=True) as tmp_f:
                                try:
                                    tmp_f.write(rz.read(path))
                                    tmp_f.flush()
                                    zo.write(tmp_f.name, path, rz.ZIP_DEFLATED)
                                except zlib.error:
                                    # Corrupted compression, which is expected
                                    pass
                                except BadZipfile:
                                    # Corrupted zip file, also expected
                                    pass

                return [[out_name, encoding, "repaired_zip_file.zip"]], False
        except ValueError:
            return [], False
        except NotImplementedError:
            # Compression type 99 is not implemented in python zipfile
            return [], False

    # noinspection PyCallingNonCallable
    def extract_office(self, request: ServiceRequest, local, encoding):
        """Will attempt to use modules in doc_extract.py to extract a document from an encrypted Office file.

        Args:
            request AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing decoded file path, encoding, and display name "[orig FH name]_decoded", or a blank list if
            decryption failed; and True if encryption successful (indicating encryption detected).
        """
        # When encrypted, AL will identify the document as an unknown office type.
        if request.tag != "document/office/unknown":
            return [], False

        passwords = self.get_passwords(request)
        try:
            # Check is msoffice is compiled
            if os.path.isfile("/opt/al/support/extract/msoffice/bin/msoffice-crypt.exe"):
                # Still going to use extract_docx as a backup for now, so try that module if msoffice fails
                try_next = True
                res = mstools(local, passwords, self.working_directory)
            else:
                try_next = False
                self.log.warning("Extract service out of date. Reinstall on workers with "
                                 "/opt/al/pkg/assemblyline/al/install/reinstall_service.py Extract")
                res = extract_docx(local, passwords, self.working_directory)

            if res is None and not try_next:
                raise ValueError()
            # Try old module if msoffice errors
            if res is None:
                res = extract_docx(local, passwords, self.working_directory)
                if res is None:
                    raise ValueError()
        except ValueError:
            # Not a valid supported/valid file
            return [], False
        except PasswordError:
            # Could not guess password
            return [], True

        out_name, password = res
        self._last_password = password
        display_name = "_decoded".join(os.path.splitext(os.path.basename(request.path)))
        return [[out_name, encoding, display_name]], True

    def _7zip_submit_extracted(self, request : ServiceRequest, path, encoding):
        """Will attempt to use 7zip library to extract content from a generic archive or PE file.

        Args:
            request AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name
            or a blank list if extraction failed.
        """
        extract_pe_sections = request.get_param('extract_pe_sections')
        extracted_children = []
        for root, _, files in os.walk(path):
            for f in files:
                filename = safe_str(os.path.join(root, f).replace(path, ""))
                if filename.startswith("/"):
                    filename = filename[1:]
                if re.match("Payload/[^/]*.app/Info.plist", safe_str(filename)):
                    self.is_ipa = True
                if not extract_pe_sections and \
                        ((encoding.startswith("executable/windows") and
                          [f2 for f2 in self.FORBIDDEN_EXE if filename.startswith(f2)]) or
                         (encoding.startswith("executable/linux")and filename in self.FORBIDDEN_ELF_EXE)):
                    raise ExtractIgnored("'Extract PE sections' option not selected. PE/ELF file sections will not "
                                         "be extracted. See service README for more details.")

                extracted_children.append([os.path.join(root, f), encoding, safe_str(filename)])

        return extracted_children

    def extract_ace(self, request: ServiceRequest, local, encoding):
        """Will attempt to use command-line tool unace to extract content from an ACE archive.

        Args:
            request AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name,
            or a blank list if extraction failed; and True if encryption with password detected.
        """
        if encoding != 'ace':
            return [], False

        path = os.path.join(self.working_directory, "ace")
        try:
            os.mkdir(path)
        except OSError:
            pass

        # noinspection PyBroadException
        try:
            with tempfile.NamedTemporaryFile(suffix=".ace", dir=path) as tf:
                # unace needs the .ace file extension
                with open(local, "rb") as fh:
                    tf.write(fh.read())
                    tf.flush()

                proc = self.st.run(subprocess.Popen(
                    '/usr/bin/unace e -y %s' % tf.name,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, cwd=path, env=os.environ, shell=True,
                    preexec_fn=set_death_signal()))

                # Note, proc.communicate() hangs
                stdoutput = proc.stdout.read()
                while True:
                    stdoutput += proc.stdout.read()
                    if proc.poll() is not None:
                        break
                    time.sleep(0.01)

            if stdoutput:
                extracted_children = []
                if "extracted:" in stdoutput:
                    for line in stdoutput.splitlines():
                        line = line.strip()
                        m = re.match("extracting (.+?)[ ]*(CRC OK)?$", line)
                        if not m:
                            continue

                        filename = m.group(1)
                        filepath = os.path.join(path, filename)
                        if os.path.isdir(filepath):
                            continue
                        else:
                            extracted_children.append([filepath, encoding, safe_str(filename)])

                return extracted_children, False

        except ExtractIgnored:
            raise
        except Exception:
            self.log.exception('While extracting %s with unace', request.srl)

        return [], False

    def extract_pdf(self, request: ServiceRequest, local, encoding):
        """Will attempt to use command-line tool pdfdetach to extract embedded files from a PDF sample.

        Args:
            request AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name,
            or a blank list if extraction failed or no embedded files are detected; and False as no passwords will
            ever be detected.
        """
        extracted_children = []

        if encoding == 'document/pdf':
            output_path = os.path.join(self.working_directory, "pdf")
            if not os.path.exists(output_path):
                os.makedirs(output_path)

            env = os.environ.copy()
            env['LANG'] = 'en_US.UTF-8'

            try:
                subprocess.Popen(
                    ['pdfdetach', '-saveall', '-o', output_path, local],
                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE).communicate()
            except Exception:
                self.log.error("Extract service needs poppler-utils to extract embedded PDF files. Install on workers "
                               "with '/opt/al/pkg/assemblyline/al/install/reinstall_service.py Extract'")
                return extracted_children, False

            files = (filename for filename in os.listdir(output_path) if
                     os.path.isfile(os.path.join(output_path, filename)))

            for filename in files:
                extracted_children.append([output_path + "/" + filename, encoding, safe_str(filename)])

        return extracted_children, False

    @staticmethod
    def decode_vbe(data):
        """Will attempt to decode VBE script. Modified code that was written by Didier Stevens, found here:
        https://blog.didierstevens.com/2016/03/29/decoding-vbe/

        Args:
            data: VBE content.

        Returns:
            Decoded script if successful, or None.
        """
        try:
            d_decode = {9: '\x57\x6E\x7B', 10: '\x4A\x4C\x41', 11: '\x0B\x0B\x0B', 12: '\x0C\x0C\x0C',
                        13: '\x4A\x4C\x41', 14: '\x0E\x0E\x0E', 15: '\x0F\x0F\x0F', 16: '\x10\x10\x10',
                        17: '\x11\x11\x11', 18: '\x12\x12\x12', 19: '\x13\x13\x13', 20: '\x14\x14\x14',
                        21: '\x15\x15\x15', 22: '\x16\x16\x16', 23: '\x17\x17\x17', 24: '\x18\x18\x18',
                        25: '\x19\x19\x19', 26: '\x1A\x1A\x1A', 27: '\x1B\x1B\x1B', 28: '\x1C\x1C\x1C',
                        29: '\x1D\x1D\x1D', 30: '\x1E\x1E\x1E', 31: '\x1F\x1F\x1F', 32: '\x2E\x2D\x32',
                        33: '\x47\x75\x30', 34: '\x7A\x52\x21', 35: '\x56\x60\x29', 36: '\x42\x71\x5B',
                        37: '\x6A\x5E\x38', 38: '\x2F\x49\x33', 39: '\x26\x5C\x3D', 40: '\x49\x62\x58',
                        41: '\x41\x7D\x3A', 42: '\x34\x29\x35', 43: '\x32\x36\x65', 44: '\x5B\x20\x39',
                        45: '\x76\x7C\x5C', 46: '\x72\x7A\x56', 47: '\x43\x7F\x73', 48: '\x38\x6B\x66',
                        49: '\x39\x63\x4E', 50: '\x70\x33\x45', 51: '\x45\x2B\x6B', 52: '\x68\x68\x62',
                        53: '\x71\x51\x59', 54: '\x4F\x66\x78', 55: '\x09\x76\x5E', 56: '\x62\x31\x7D',
                        57: '\x44\x64\x4A', 58: '\x23\x54\x6D', 59: '\x75\x43\x71', 60: '\x4A\x4C\x41',
                        61: '\x7E\x3A\x60', 62: '\x4A\x4C\x41', 63: '\x5E\x7E\x53', 64: '\x40\x4C\x40',
                        65: '\x77\x45\x42', 66: '\x4A\x2C\x27', 67: '\x61\x2A\x48', 68: '\x5D\x74\x72',
                        69: '\x22\x27\x75', 70: '\x4B\x37\x31', 71: '\x6F\x44\x37', 72: '\x4E\x79\x4D',
                        73: '\x3B\x59\x52', 74: '\x4C\x2F\x22', 75: '\x50\x6F\x54', 76: '\x67\x26\x6A',
                        77: '\x2A\x72\x47', 78: '\x7D\x6A\x64', 79: '\x74\x39\x2D', 80: '\x54\x7B\x20',
                        81: '\x2B\x3F\x7F', 82: '\x2D\x38\x2E', 83: '\x2C\x77\x4C', 84: '\x30\x67\x5D',
                        85: '\x6E\x53\x7E', 86: '\x6B\x47\x6C', 87: '\x66\x34\x6F', 88: '\x35\x78\x79',
                        89: '\x25\x5D\x74', 90: '\x21\x30\x43', 91: '\x64\x23\x26', 92: '\x4D\x5A\x76',
                        93: '\x52\x5B\x25', 94: '\x63\x6C\x24', 95: '\x3F\x48\x2B', 96: '\x7B\x55\x28',
                        97: '\x78\x70\x23', 98: '\x29\x69\x41', 99: '\x28\x2E\x34', 100: '\x73\x4C\x09',
                        101: '\x59\x21\x2A', 102: '\x33\x24\x44', 103: '\x7F\x4E\x3F', 104: '\x6D\x50\x77',
                        105: '\x55\x09\x3B', 106: '\x53\x56\x55', 107: '\x7C\x73\x69', 108: '\x3A\x35\x61',
                        109: '\x5F\x61\x63', 110: '\x65\x4B\x50', 111: '\x46\x58\x67', 112: '\x58\x3B\x51',
                        113: '\x31\x57\x49', 114: '\x69\x22\x4F', 115: '\x6C\x6D\x46', 116: '\x5A\x4D\x68',
                        117: '\x48\x25\x7C', 118: '\x27\x28\x36', 119: '\x5C\x46\x70', 120: '\x3D\x4A\x6E',
                        121: '\x24\x32\x7A', 122: '\x79\x41\x2F', 123: '\x37\x3D\x5F', 124: '\x60\x5F\x4B',
                        125: '\x51\x4F\x5A', 126: '\x20\x42\x2C', 127: '\x36\x65\x57'}

            d_combination = {0: 0, 1: 1, 2: 2, 3: 0, 4: 1, 5: 2, 6: 1, 7: 2, 8: 2, 9: 1, 10: 2, 11: 1, 12: 0, 13: 2,
                             14: 1, 15: 2, 16: 0, 17: 2, 18: 1, 19: 2, 20: 0, 21: 0, 22: 1, 23: 2, 24: 2, 25: 1, 26: 0,
                             27: 2, 28: 1, 29: 2, 30: 2, 31: 1, 32: 0, 33: 0, 34: 2, 35: 1, 36: 2, 37: 1, 38: 2, 39: 0,
                             40: 2, 41: 0, 42: 0, 43: 1, 44: 2, 45: 0, 46: 2, 47: 1, 48: 0, 49: 2, 50: 1, 51: 2, 52: 0,
                             53: 0, 54: 1, 55: 2, 56: 2, 57: 0, 58: 0, 59: 1, 60: 2, 61: 0, 62: 2, 63: 1}

            result = ''
            index = -1
            for char in data \
                    .replace('@&', chr(10)) \
                    .replace('@#', chr(13)) \
                    .replace('@*', '>') \
                    .replace('@!', '<') \
                    .replace('@$', '@'):
                byte = ord(char)
                if byte < 128:
                    index += 1
                if (byte == 9 or 31 < byte < 128) and byte != 60 and byte != 62 and byte != 64:
                    char = [c for c in d_decode[byte]][d_combination[index % 64]]
                result += char
            return result
        except Exception:
            result = None
            return result

    def extract_vbe(self, _, local, encoding):
        """Will attempt to decode VBA code data from a VBE container.

        Args:
            _: Unused AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing decoded file information, including: decoded file path, encoding, and display name,
            or a blank list if decode failed; and False (no passwords will ever be detected).
        """
        if encoding == 'code/vbe':
            with open(local, "rb") as fh:
                text = fh.read()
            try:
                # Ensure file format is correct via regex
                evbe_regex = re.compile(r'#@~\^......==(.+)......==\^#~@')
                evbe_present = re.search(evbe_regex, text)
                evbe_res = self.decode_vbe(evbe_present.groups()[0])
                if evbe_res and evbe_present != text:
                    path = os.path.join(self.working_directory, 'vbe_decoded')
                    with open(path, 'wb') as f:
                        f.write(evbe_res)
                    return [[path, encoding, 'vbe_decoded']], False
            except Exception:
                pass
        return [], False

    def extract_7zip(self, request: ServiceRequest, local, encoding):
        """Will attempt to use 7zip and then unrar to extract content from an archive, or sections from a Windows
        executable file.

        Args:
            request: AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name,
            or a blank list if extraction failed; and True if encryption detected.
        """
        password_protected = False

        # If we cannot extract the file, we shouldn't pass it around. Let keep track of if we can't.
        password_failed = False
        if request.tag == 'archive/audiovisual/flash' or encoding == 'ace' or request.tag.startswith('document') or \
                encoding == 'tnef':
            return [], password_protected
        path = os.path.join(request.working_directory, "7zip")
        # noinspection PyBroadException
        try:
            env = os.environ.copy()
            env['LANG'] = 'en_US.UTF-8'

            stdoutput, stderr = subprocess.Popen(
                ['7z', 'x', '-p', '-y', local, '-o%s' % path],
                env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE).communicate()
            stdoutput += stderr
            if stdoutput and stdoutput.strip().find("Everything is Ok") > 0:
                return self._7zip_submit_extracted(request, path, encoding), password_protected
            else:
                if "Wrong password" in stdoutput:
                    password_protected = True
                    password_list = self.get_passwords(request)
                    for password in password_list:
                        try:
                            shutil.rmtree(path, ignore_errors=True)
                            stdoutput, stderr = subprocess.Popen([
                                '7za', 'x', '-p%s' % password,
                                            '-o%s' % path, local
                            ], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE).communicate()
                            stdoutput += stderr

                            if stdoutput and"\nEverything is Ok\n" in stdoutput:
                                self._last_password = password
                                return self._7zip_submit_extracted(request, path, encoding), password_protected
                        except OSError:
                            pass
                    password_failed = True

            # Try unrar if 7zip fails for rar archives
            if encoding == 'rar':
                password_protected = False

                # Resetting back to False because we are giving it another chance.
                password_failed = False
                shutil.rmtree(path, ignore_errors=True)
                os.mkdir(path)
                try:
                    stdoutrar, stderrrar = subprocess.Popen(
                        ['unrar', 'x', '-y', '-p-', local, path],
                        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate()
                except OSError:
                    self.log.warning("Error running unrar on sample {}. Extract service may be out of date. Reinstall"
                                     "with /opt/al/pkg/assemblyline/al/install/reinstall_service Extract"
                                     .format(self.sha))
                    stdoutrar = None
                    stderrrar = None
                if stdoutrar:
                    if 'All OK' in stdoutrar:
                        return self._7zip_submit_extracted(request, path, encoding), password_protected
                    if 'wrong password' in stderrrar:
                        password_protected = True
                        password_list = self.get_passwords(request)
                        for password in password_list:
                            try:
                                shutil.rmtree(path, ignore_errors=True)
                                os.mkdir(path)
                                proc, _ = subprocess.Popen(
                                    ['unrar', 'x', '-y', '-p{}' .format(password), local, path],
                                    env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE).communicate()
                                if "All OK" in proc:
                                    self._last_password = password
                                    return self._7zip_submit_extracted(request, path, encoding), password_protected
                            except OSError:
                                pass
                    password_failed = True
        except ExtractIgnored:
            raise
        except Exception:
            if request.tag != 'archive/cab':
                self.log.exception('While extracting %s with 7zip', request.srl)
        if password_failed:
            # stop processing the request
            request.drop()

        return [], password_protected

    def extract_swf(self, request: ServiceRequest, file_path, encoding):
        """Will attempt to extract compressed SWF files.

        Args:
            request: Unused AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name,
            or a blank list if extract failed; and False (no passwords will ever be detected).
        """
        extracted_children = []

        if encoding == 'audiovisual/flash':
            output_path = os.path.join(request.working_directory, "swf")
            if not os.path.exists(output_path):
                os.makedirs(output_path)

            files_found = []
            # noinspection PyBroadException
            try:
                swf = xxxswf()
                files_found = swf.extract(file_path, output_path)
            except ImportError:
                self.log.exception("Import error: pylzma library not installed.")
            except Exception:
                self.log.exception("Error occurred while trying to decompress swf...")

            for child in files_found:
                extracted_children.append([output_path + "/" + child, encoding, child])

        return extracted_children, False

    def extract_tnef(self, request: ServiceRequest, file_path, encoding):
        """Will attempt to extract data from a TNEF container.

        Args:
            _: Unused AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted file information, including: extracted path, encoding, and display name,
            or a blank list if extract failed; and False (no passwords will ever be detected).
        """
        children = []

        if encoding != 'tnef':
            return children, False

        # noinspection PyBroadException
        try:
            # noinspection PyUnresolvedReferences
            from tnefparse import tnef
            tnef_logger = logging.getLogger("tnef-decode")
            tnef_logger.setLevel(60)  # This completely turns off the TNEF logger

            count = 0
            for a in tnef.TNEF(open(file_path).read()).attachments:
                # This may not exist so try to access it and deal the
                # possible AttributeError, by skipping this entry as
                # there is no point if there is no data.
                try:
                    data = a.data
                except AttributeError:
                    continue

                count += 1

                # This may not exist either but long_filename still
                # seems to return so deal with the AttributeError
                # here rather than blowing up.
                try:
                    name = a.long_filename() or a.name
                    if not name:
                        continue

                    name = safe_str(name)
                except AttributeError:
                    name = 'unknown_tnef_%d' % count

                if not name:
                    continue

                path = os.path.join(request.working_directory, str(count))
                with open(path, 'w') as f:
                    f.write(data)

                children.append([path, encoding, name])
        except ImportError:
            self.log.exception("Import error: tnefparse library not installed:")
        except Exception:
            self.log.exception("Error extracting from tnef file:")

        return children, False

    @staticmethod
    def jar_whitelisting(extracted, whitelisted_count, encoding):
        """Filters file paths that are considered whitelisted from a list of extracted JAR files.

        Args:
            extracted: List of extracted file information, including: extracted path, encoding, and display name.
            whitelisted_count: Current whitelist count.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List of filtered file names and updated count of whitelisted files.
        """
        if encoding == "java/jar" or encoding == "android/apk":
            whitelisted_tags_re = [
                re.compile(r'android/(xml|dex|resource)'),
                re.compile(r'audiovisual/.*'),
                re.compile(r'certificate/rsa'),
                re.compile(r'code/.*'),
                re.compile(r'db/.*'),
                re.compile(r'font/.*'),
                re.compile(r'image/.*'),
                re.compile(r'java/(class|manifest|jbdiff|signature)'),
                re.compile(r'resource/.*'),
            ]

            whitelisted_mime_re = [
                re.compile(r'text/plain'),
                re.compile(r'text/x-c'),
            ]

            whitelisted_fname_regex = [
                # commonly used libs files
                re.compile(r'com/google/i18n/phonenumbers/data/(PhoneNumber|ShortNumber)[a-zA-Z]*_[0-9A-Z]{1,3}$'),
                re.compile(r'looksery/([a-zA-Z_]*/){1,5}[a-zA-Z0-9_.]*.glsl$'),
                re.compile(r'org/apache/commons/codec/language/bm/[a-zA-Z0-9_.]*\.txt$'),
                re.compile(r'org/joda/time/format/messages([a-zA-Z_]{0,3})\.properties$'),
                re.compile(r'org/joda/time/tz/data/[a-zA-Z0-9_/\-+]*$'),
                re.compile(r'sharedassets[0-9]{1,3}\.assets(\.split[0-9]{1,3})?$'),
                re.compile(r'zoneinfo(-global)?/([a-zA-Z_\-]*/){1,2}[a-zA-Z_\-]*\.ics$'),

                # noisy files
                re.compile(r'assets/.*\.atf$'),
                re.compile(r'assets/.*\.ffa$'),
                re.compile(r'assets/.*\.ffm$'),
                re.compile(r'assets/.*\.jsa$'),
                re.compile(r'assets/.*\.lua$'),
                re.compile(r'assets/.*\.pf$'),
            ]

            jar_filter_count = whitelisted_count
            tmp_new_files = []

            for cur_file in extracted:
                f = open(cur_file[0], "rb")
                byte_block = f.read(65535 * 2)
                f.close()

                to_add = True
                file_info = ident(byte_block, len(byte_block))
                for exp in whitelisted_tags_re:
                    if exp.search(file_info['tag']):
                        if DEBUG:
                            print("T", file_info['tag'], file_info['ascii'], cur_file[0])
                        to_add = False
                        jar_filter_count += 1

                if to_add:
                    for exp in whitelisted_mime_re:
                        if exp.search(file_info['mime']):
                            if DEBUG:
                                print("M", file_info['mime'], file_info['ascii'], cur_file[0])
                            to_add = False
                            jar_filter_count += 1

                if to_add:
                    for ext in whitelisted_fname_regex:
                        if ext.search(cur_file[0]):
                            if DEBUG:
                                print("F", ext.pattern, file_info['ascii'], cur_file[0])
                            to_add = False
                            jar_filter_count += 1

                if to_add:
                    tmp_new_files.append(cur_file)

            return tmp_new_files, jar_filter_count
        else:
            return extracted, whitelisted_count

    @staticmethod
    def archive_with_executables(request: ServiceRequest, result):
        """Detects executable files contained in an archive using the service's LAUNCHABLE_EXTENSIONS list.

        Args:
            request: AL request object.
            result: AL result object.

        Returns:
            Al result object scoring VHIGH if executables detected in container, or None.
        """
        if len(request.extracted) == 1 and \
                os.path.splitext(request.extracted[0].display_name)[1].lower() in Extract.LAUNCHABLE_EXTENSIONS:

            new_section = ResultSection(SCORE.VHIGH,
                                        "Archive file with single executable inside. Potentially malicious...")
            result.add_section(new_section)
            result.add_tag('file.behavior', "Archived Single Executable", TAG_WEIGHT['MED'])
        else:
            for extracted in request.extracted:
                if os.path.splitext(extracted.display_name)[1].lower() in Extract.LAUNCHABLE_EXTENSIONS:
                    result.add_tag('file.behavior', "Executable Content in Archive", TAG_WEIGHT['MED'])
                    break

    @staticmethod
    def archive_is_arc(request: ServiceRequest, result):
        """Uses AL tag to determine if container is an ACE archive.

        Args:
            request: AL request object.
            result: AL result object.

        Returns:
            Al result object scoring VHIGH if sample type is ACE container, or None.
        """
        if request.tag == 'archive/ace':
            result.add_section(ResultSection(score=SCORE.VHIGH, title_text="Uncommon format: archive/ace"))
            result.add_tag('file.behavior', "Uncommon format: archive/ace", TAG_WEIGHT['MED'])

    @staticmethod
    def yield_eml_parts(message):
        """Parses EML container to collect attachment information and EML body content.

        Args:
            message: EML container.

        Returns:
            A tuple of attachment information, including: content type, content disposition, decoded content, filename
            and content charset.
        """
        if message.is_multipart():
            for part in message.walk():
                p_type = part.get_content_type()
                p_disp = part.get("Content-Disposition", "")
                p_load = part.get_payload(decode=True)
                p_name = part.get_filename(None)
                p_cset = part.get_content_charset()
                yield (p_type, p_disp, p_load, p_name, p_cset)
        else:
            p_type = message.get_content_type()
            p_disp = message.get("Content-Disposition", "")
            p_load = message.get_payload(decode=True)
            p_name = message.get_filename(None)
            p_cset = message.get_content_charset()
            yield (p_type, p_disp, p_load, p_name, p_cset)

    # noinspection PyCallingNonCallable
    def extract_eml(self, request: ServiceRequest, local, encoding):
        """Will attempt to extract attachments from an EML container. Also collects strings of EML body.

        Args:
            request: Unused AL request object.
            local: File path of AL sample.
            encoding: AL tag with string 'archive/' replaced.

        Returns:
            List containing extracted attachment information, including: extracted path, encoding, display name,
            classification and email body strings (as dict); and False (no encryption will be detected).
        """
        # Allow attachments to be extracted from html emails
        if encoding == "document/email" or encoding == "code/html":
            pass
        else:
            return [], False

        extracted = []
        body_words = set()
        with open(local, "r") as fh:
            message = email.message_from_file(fh)
            for part_num, (p_t, p_d, p_l, p_n, p_c) in enumerate(self.yield_eml_parts(message)):
                is_body = not p_n and "attachment" not in p_d and p_t != "application/octet-stream"
                if p_l is None or p_l.strip() == "":
                    continue
                if is_body:
                    encoding = p_c or "utf-8"
                    try:
                        try:
                            body = unicode(p_l, encoding=encoding)
                        except LookupError:
                            # This is usually old windows codepages or jibberish
                            body = unicode(p_l, encoding="utf8", errors="ignore")
                            self.log.info("Detected non-supported codepage %s" % encoding)
                        if p_t == "text/html":
                            try:
                                body = html.document_fromstring(body).text_content()
                            except ValueError:
                                # For documents with xml encoding declarations
                                body = html.document_fromstring(p_l).text_content()
                            except etree.ParserError:
                                # If /body is empty, just grab the text
                                body = BeautifulSoup(body).text
                        if len(body) > 0:
                            # Go through once separating by whitespace
                            words = re.findall("[^ \n\t\r\xa0]+", body)
                            body_words.update(words)
                            # Go through again separating by ANY special character
                            words = re.findall("[A-Za-z0-9]+", body)
                            body_words.update(words)
                    except UnicodeDecodeError:
                        # cannot decode body by specified content
                        pass

                if self.max_attachment_size is not None and len(p_l) > self.max_attachment_size:
                    continue
                if self.named_attachments_only and is_body:
                    continue
                elif p_n is None:
                    p_n = "email_part_%i" % part_num

                ft = tempfile.NamedTemporaryFile(dir=request.working_directory, delete=False)
                ft.write(p_l)
                name = ft.name
                ft.close()
                extracted.append((name, p_t, p_n, self.SERVICE_CLASSIFICATION, {'email_body': list(body_words)}))

        return extracted, False
