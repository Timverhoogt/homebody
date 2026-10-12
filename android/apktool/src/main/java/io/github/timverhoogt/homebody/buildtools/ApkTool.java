package io.github.timverhoogt.homebody.buildtools;

import com.android.apksig.ApkSigner;
import com.android.apksig.ApkVerifier;
import com.android.apksig.KeyConfig;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.RandomAccessFile;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.cert.Certificate;
import java.security.cert.X509Certificate;
import java.time.LocalDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Enumeration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.zip.CRC32;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;
import java.util.zip.ZipOutputStream;

/**
 * Host-side APK packaging, signing and verification.
 *
 * <pre>
 * package --resources res.ap_ --dex classes.dex --out unsigned.apk
 * sign    --in unsigned.apk --out signed.apk --keystore k.p12 --password-file pw --min-sdk 26
 * verify  --apk signed.apk --min-sdk 26
 * </pre>
 *
 * Signing uses apksig, the same library that backs the SDK's apksigner and AGP.
 * The keystore password is read from a file so it never appears in a command line.
 */
public final class ApkTool {
    // Fixed timestamp keeps unsigned APK bytes reproducible for identical inputs.
    private static final LocalDateTime ENTRY_TIME = LocalDateTime.of(1981, 1, 1, 1, 1, 2);

    private ApkTool() {}

    public static void main(String[] args) throws Exception {
        if (args.length == 0) throw usage();
        Map<String, String> options = parseOptions(args);
        switch (args[0]) {
            case "package":
                packageApk(file(options, "resources"), file(options, "dex"), file(options, "out"));
                break;
            case "sign":
                sign(file(options, "in"), file(options, "out"), file(options, "keystore"),
                        file(options, "password-file"), Integer.parseInt(required(options, "min-sdk")));
                break;
            case "verify":
                verify(file(options, "apk"), Integer.parseInt(required(options, "min-sdk")));
                break;
            default:
                throw usage();
        }
    }

    static void packageApk(File resources, File dex, File out) throws IOException {
        Files.createDirectories(out.getParentFile().toPath());
        try (ZipFile resourceApk = new ZipFile(resources);
             ZipOutputStream zip = new ZipOutputStream(new FileOutputStream(out))) {
            Enumeration<? extends ZipEntry> entries = resourceApk.entries();
            while (entries.hasMoreElements()) {
                ZipEntry source = entries.nextElement();
                if (source.getName().equals("classes.dex")) continue;
                try (InputStream in = resourceApk.getInputStream(source)) {
                    // aapt2 stores resources.arsc and media uncompressed; keep each entry's method.
                    writeEntry(zip, source.getName(), in.readAllBytes(), source.getMethod() == ZipEntry.STORED);
                }
            }
            writeEntry(zip, "classes.dex", Files.readAllBytes(dex.toPath()), false);
        }
    }

    private static void writeEntry(ZipOutputStream zip, String name, byte[] data, boolean stored)
            throws IOException {
        ZipEntry entry = new ZipEntry(name);
        entry.setTimeLocal(ENTRY_TIME);
        if (stored) {
            CRC32 crc = new CRC32();
            crc.update(data);
            entry.setMethod(ZipEntry.STORED);
            entry.setSize(data.length);
            entry.setCompressedSize(data.length);
            entry.setCrc(crc.getValue());
        } else {
            entry.setMethod(ZipEntry.DEFLATED);
        }
        zip.putNextEntry(entry);
        zip.write(data);
        zip.closeEntry();
    }

    static void sign(File in, File out, File keystoreFile, File passwordFile, int minSdk) throws Exception {
        char[] password = new String(Files.readAllBytes(passwordFile.toPath()), StandardCharsets.UTF_8)
                .trim().toCharArray();
        KeyStore keystore = KeyStore.getInstance("PKCS12");
        try (FileInputStream stream = new FileInputStream(keystoreFile)) {
            keystore.load(stream, password);
        }
        String alias = keystore.aliases().nextElement();
        PrivateKey key = (PrivateKey) keystore.getKey(alias, password);
        List<X509Certificate> chain = new ArrayList<>();
        for (Certificate certificate : keystore.getCertificateChain(alias)) {
            chain.add((X509Certificate) certificate);
        }
        ApkSigner.SignerConfig signer = new ApkSigner.SignerConfig.Builder("HOMEBODY", new KeyConfig.Jca(key), chain).build();
        Files.createDirectories(out.getParentFile().toPath());
        new ApkSigner.Builder(List.of(signer))
                .setInputApk(in)
                .setOutputApk(out)
                .setMinSdkVersion(minSdk)
                // minSdk 26 needs no JAR signature; v2 and v3 cover every supported release.
                .setV1SigningEnabled(false)
                .setV2SigningEnabled(true)
                .setV3SigningEnabled(true)
                .build()
                .sign();
        Arrays.fill(password, '\0');
    }

    static void verify(File apk, int minSdk) throws Exception {
        ApkVerifier.Result result = new ApkVerifier.Builder(apk)
                .setMinCheckedPlatformVersion(minSdk)
                .build()
                .verify();
        System.out.println("APK: " + apk.getAbsolutePath());
        System.out.println("Verified: " + result.isVerified());
        System.out.println("Verified using v1 scheme (JAR signing): " + result.isVerifiedUsingV1Scheme());
        System.out.println("Verified using v2 scheme (APK Signature Scheme v2): " + result.isVerifiedUsingV2Scheme());
        System.out.println("Verified using v3 scheme (APK Signature Scheme v3): " + result.isVerifiedUsingV3Scheme());
        System.out.println("Number of signers: " + result.getSignerCertificates().size());
        for (X509Certificate certificate : result.getSignerCertificates()) {
            System.out.println("Signer certificate DN: " + certificate.getSubjectX500Principal().getName());
            System.out.println("Signer certificate SHA-256 digest: " + hex(sha256(certificate.getEncoded())));
        }
        result.getErrors().forEach(issue -> System.out.println("ERROR: " + issue));
        result.getWarnings().forEach(issue -> System.out.println("WARNING: " + issue));
        for (ApkVerifier.Result.V2SchemeSignerInfo signerInfo : result.getV2SchemeSigners()) {
            signerInfo.getErrors().forEach(issue -> System.out.println("ERROR (v2): " + issue));
        }
        for (ApkVerifier.Result.V3SchemeSignerInfo signerInfo : result.getV3SchemeSigners()) {
            signerInfo.getErrors().forEach(issue -> System.out.println("ERROR (v3): " + issue));
        }
        List<String> misaligned = misalignedStoredEntries(apk);
        System.out.println("Uncompressed entries 4-byte aligned: " + misaligned.isEmpty());
        misaligned.forEach(name -> System.out.println("MISALIGNED: " + name));
        System.out.println("APK SHA-256: " + hex(sha256(Files.readAllBytes(apk.toPath()))));
        if (!result.isVerified() || !misaligned.isEmpty()) {
            throw new IllegalStateException("APK verification failed");
        }
    }

    /** Walks the central directory and checks the data offset of every STORED entry. */
    static List<String> misalignedStoredEntries(File apk) throws IOException {
        List<String> misaligned = new ArrayList<>();
        try (RandomAccessFile file = new RandomAccessFile(apk, "r")) {
            long eocd = findEndOfCentralDirectory(file);
            int count = readUnsignedShort(file, eocd + 10);
            long offset = readUnsignedInt(file, eocd + 16);
            for (int i = 0; i < count; i++) {
                if (readUnsignedInt(file, offset) != 0x02014b50L) throw new IOException("Bad central directory");
                int method = readUnsignedShort(file, offset + 10);
                int nameLength = readUnsignedShort(file, offset + 28);
                int extraLength = readUnsignedShort(file, offset + 30);
                int commentLength = readUnsignedShort(file, offset + 32);
                long localHeader = readUnsignedInt(file, offset + 42);
                byte[] name = new byte[nameLength];
                file.seek(offset + 46);
                file.readFully(name);
                long dataOffset = localHeader + 30
                        + readUnsignedShort(file, localHeader + 26)
                        + readUnsignedShort(file, localHeader + 28);
                if (method == ZipEntry.STORED && dataOffset % 4 != 0) {
                    misaligned.add(new String(name, StandardCharsets.UTF_8));
                }
                offset += 46 + nameLength + extraLength + commentLength;
            }
        }
        return misaligned;
    }

    private static long findEndOfCentralDirectory(RandomAccessFile file) throws IOException {
        long minimum = Math.max(0, file.length() - 22 - 0xffff);
        for (long position = file.length() - 22; position >= minimum; position--) {
            if (readUnsignedInt(file, position) == 0x06054b50L) return position;
        }
        throw new IOException("No end of central directory record");
    }

    private static int readUnsignedShort(RandomAccessFile file, long position) throws IOException {
        file.seek(position);
        return file.read() | (file.read() << 8);
    }

    private static long readUnsignedInt(RandomAccessFile file, long position) throws IOException {
        file.seek(position);
        long value = 0;
        for (int i = 0; i < 4; i++) value |= ((long) file.read()) << (8 * i);
        return value;
    }

    private static byte[] sha256(byte[] data) throws Exception {
        return MessageDigest.getInstance("SHA-256").digest(data);
    }

    private static String hex(byte[] bytes) {
        StringBuilder builder = new StringBuilder();
        for (byte b : bytes) builder.append(String.format("%02x", b));
        return builder.toString();
    }

    private static Map<String, String> parseOptions(String[] args) {
        Map<String, String> options = new HashMap<>();
        for (int i = 1; i + 1 < args.length; i += 2) {
            if (!args[i].startsWith("--")) throw usage();
            options.put(args[i].substring(2), args[i + 1]);
        }
        return options;
    }

    private static String required(Map<String, String> options, String name) {
        String value = options.get(name);
        if (value == null) throw new IllegalArgumentException("Missing --" + name);
        return value;
    }

    private static File file(Map<String, String> options, String name) {
        return new File(required(options, name));
    }

    private static IllegalArgumentException usage() {
        return new IllegalArgumentException("usage: ApkTool package|sign|verify --option value ...");
    }
}
